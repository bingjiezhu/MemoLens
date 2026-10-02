import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  AGENT_AUTHORITY_REQUEST_TIMEOUT_MS,
  AgentAuthorityCoordinator,
  canonicalizeNativeDecisionContent,
  conductNativeDecisionReview,
  DesktopAuthorityError,
  paginateNativeDecisionContent,
  sanitizeNativeAuthorityText,
} from "../electron-dist/electron/agentAuthorityCoordinator.js";
import {
  getDesktopSessionToken,
  getMainAuthorityToken,
  getRuntimeAuthorityEpoch,
  revokeBackendTrust,
} from "../electron-dist/electron/backendManager.js";
import { BackendProcessSupervisor } from "../electron-dist/electron/backendProcessSupervisor.js";
import { managedPythonCommand } from "../electron-dist/electron/desktopSettings.js";

const digest = "ab".repeat(32);
const projectId = "project-1";
const pairingId = "pairing-1";
const capabilityId = "capability-1";
const decisionNames = [
  "intent_goal",
  "intent_stance",
  "script",
  "creative_direction",
  "output",
  "material_constraints",
  "references",
  "techniques",
];

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function projection(confirmed = []) {
  return {
    object: "creative_blueprint.revision",
    schema_version: "1",
    project_id: projectId,
    revision: 3,
    creation_authority: { state: "unverified", verified: false },
    authority_projection: {
      state: confirmed.length === 0
        ? "unverified"
        : confirmed.length === decisionNames.length
          ? "confirmed"
          : "partially_confirmed",
      confirmed_decision_unit_count: confirmed.length,
      decision_unit_count: 8,
      as_of_revision: 3,
      as_of_content_sha256: digest,
      decision_units: Object.fromEntries(decisionNames.map((name) => [name, {
        state: confirmed.includes(name) ? "confirmed" : "unverified",
        confirmed: confirmed.includes(name),
        semantic_sha256: digest,
        active_confirmation: confirmed.includes(name) ? {
          event_id: "authority-event-1",
          confirmed_at: "2026-08-22T12:00:00Z",
          observed_revision: 3,
          presentation_sha256: digest,
        } : null,
      }])),
    },
  };
}

function observedBlueprintHead() {
  return {
    project_id: projectId,
    revision: 3,
    content_sha256: digest,
    semantic_sha256: digest,
    operation_id: "blueprint-operation-3",
  };
}

function observedTimelineHead() {
  return {
    revision: 2,
    revision_sha256: digest,
    timeline_id: "timeline-1",
    timeline_content_sha256: digest,
    blueprint_binding: {
      revision: 3,
      content_sha256: digest,
      semantic_sha256: digest,
      operation_id: "blueprint-operation-3",
    },
    coverage_binding: {
      revision: 4,
      content_sha256: digest,
      evidence_manifest_sha256: digest,
      operation_id: "coverage-operation-4",
    },
    operation_id: "timeline-operation-2",
  };
}

function listPayloads() {
  return {
    pairings: {
      pairings: [{
        pairing_id: pairingId,
        project_id: projectId,
        paired_subject_id: "subject-1",
        claimed_client_label: "Codex (claimed)",
        vendor_identity_verified: false,
        user_authority_verified: false,
        requested_actions: ["blueprint.commit_proposal"],
        requested_ttl_seconds: 900,
        requested_max_operations: 20,
        short_code: "K7Q2-J9",
        observed_head: { revision: 3, content_sha256: digest },
        expires_at: "2026-08-22T12:15:00Z",
        status: "pending",
      }],
    },
    capabilities: {
      capabilities: [{
        capability_id: capabilityId,
        project_id: projectId,
        paired_subject_id: "subject-1",
        claimed_client_label: "Codex (claimed)",
        vendor_identity_verified: false,
        user_authority_verified: false,
        actions: ["blueprint.commit_proposal"],
        issued_at: "2026-08-22T12:00:00Z",
        expires_at: "2026-08-22T12:15:00Z",
        max_operations: 20,
        used_operations: 2,
        remaining_operations: 18,
        status: "active",
      }],
    },
  };
}

function harness(route, presenter = {}) {
  const calls = [];
  let trustChecks = 0;
  const coordinator = new AgentAuthorityCoordinator({
    apiBase: "http://127.0.0.1:5519",
    ensureBackendTrusted: async (forceIdentityProof) => {
      trustChecks += 1;
      if (presenter.ensureBackendTrusted) {
        await presenter.ensureBackendTrusted(forceIdentityProof, trustChecks);
      }
    },
    getMainAuthorityToken: () => "main-policy-secret",
    getDesktopSessionToken: () => "desktop-session-secret",
    presenter: {
      reviewPairing: presenter.reviewPairing ?? (async () => "cancel"),
      confirmCapabilityRevoke: presenter.confirmCapabilityRevoke ?? (async () => false),
      reviewDecision: presenter.reviewDecision ?? (async () => false),
    },
    fetchImpl: async (url, init = {}) => {
      calls.push({ url: String(url), init });
      return route(new URL(String(url)).pathname, init);
    },
    now: () => new Date("2026-08-22T12:01:00Z"),
  });
  return {
    coordinator,
    calls,
    get trustChecks() {
      return trustChecks;
    },
  };
}

async function withManualTimeoutClock(run) {
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const timers = [];
  globalThis.setTimeout = (callback, delay, ...args) => {
    const timer = {
      delay,
      cleared: false,
      fired: false,
      fire() {
        assert.equal(this.fired, false, "a timeout may fire only once");
        this.fired = true;
        callback(...args);
      },
    };
    timers.push(timer);
    return timer;
  };
  globalThis.clearTimeout = (timer) => {
    timer.cleared = true;
  };
  try {
    return await run(timers);
  } finally {
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }
}

async function waitForCondition(predicate, message) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (predicate()) return;
    await Promise.resolve();
  }
  assert.fail(message);
}

test("authority coordinator accepts only exact IPv4 loopback", () => {
  const base = {
    ensureBackendTrusted: async () => {},
    getMainAuthorityToken: () => "main",
    getDesktopSessionToken: () => "desktop",
    presenter: {
      reviewPairing: async () => "cancel",
      confirmCapabilityRevoke: async () => false,
      reviewDecision: async () => false,
    },
  };
  assert.throws(
    () => new AgentAuthorityCoordinator({ ...base, apiBase: "http://localhost:5519" }),
    /exact IPv4 loopback/,
  );
  assert.throws(
    () => new AgentAuthorityCoordinator({ ...base, apiBase: "https://127.0.0.1:5519" }),
    /exact IPv4 loopback/,
  );
  assert.throws(
    () => new AgentAuthorityCoordinator({ ...base, apiBase: "http://127.0.0.1:5519/proxy" }),
    /exact IPv4 loopback/,
  );
});

test("main list fetch never sends policy authority through Origin or desktop headers", async () => {
  const payloads = listPayloads();
  const state = harness((path) => {
    if (path === "/v1/main/agent/pairings") return jsonResponse(payloads.pairings);
    if (path === "/v1/main/agent/capabilities") return jsonResponse(payloads.capabilities);
    if (path === `/v1/creative/projects/${projectId}/blueprint`) return jsonResponse(projection(["script"]));
    throw new Error(`unexpected path ${path}`);
  });

  const result = await state.coordinator.list(projectId);

  assert.equal(result.projectAuthority.confirmedDecisionUnitCount, 1);
  assert.equal(result.pairings[0].vendorIdentityVerified, false);
  assert.equal(JSON.stringify(result).includes("main-policy-secret"), false);
  assert.equal(JSON.stringify(result).includes(digest), false);
  for (const call of state.calls.slice(0, 2)) {
    const headers = new Headers(call.init.headers);
    assert.equal(headers.get("X-MemoLens-Main-Authority"), "main-policy-secret");
    assert.equal(headers.has("X-MemoLens-Desktop-Token"), false);
    assert.equal(headers.has("Origin"), false);
  }
  const blueprintHeaders = new Headers(state.calls[2].init.headers);
  assert.equal(blueprintHeaders.get("X-MemoLens-Desktop-Token"), "desktop-session-secret");
  assert.equal(blueprintHeaders.has("X-MemoLens-Main-Authority"), false);
});

test("main and desktop authority reads use the fixed deadline and clear every successful timer", async () => {
  await withManualTimeoutClock(async (timers) => {
    const payloads = listPayloads();
    const state = harness((path) => {
      if (path === "/v1/main/agent/pairings") return jsonResponse(payloads.pairings);
      if (path === "/v1/main/agent/capabilities") return jsonResponse(payloads.capabilities);
      if (path === `/v1/creative/projects/${projectId}/blueprint`) return jsonResponse(projection());
      throw new Error(`unexpected path ${path}`);
    });

    await state.coordinator.list(projectId);

    assert.equal(AGENT_AUTHORITY_REQUEST_TIMEOUT_MS, 12_000);
    assert.deepEqual(
      timers.map((timer) => timer.delay),
      [12_000, 12_000, 12_000],
    );
    assert.equal(timers.every((timer) => timer.cleared && !timer.fired), true);
    const signals = state.calls.map((call) => call.init.signal);
    assert.equal(signals.every((signal) => signal instanceof AbortSignal && !signal.aborted), true);
    assert.equal(new Set(signals).size, signals.length, "each exchange must own its deadline signal");
  });
});

test("pairing, revoke, and decision presentation timeouts fail closed before native review or mutation", async () => {
  const scenarios = [
    {
      name: "pairing",
      path: `/v1/main/agent/pairings/${pairingId}/presentation`,
      run: (coordinator) => coordinator.reviewPairing(pairingId, projectId),
    },
    {
      name: "capability revoke",
      path: `/v1/main/agent/capabilities/${capabilityId}/revoke/presentation`,
      run: (coordinator) => coordinator.revokeCapability(capabilityId, projectId),
    },
    {
      name: "decision authority",
      path: `/v1/main/creative/projects/${projectId}/blueprint/authority/presentation`,
      run: (coordinator) => coordinator.requestDecisionReview({
        projectId,
        operation: "confirm",
        decisionUnits: ["script"],
      }),
    },
  ];

  for (const scenario of scenarios) {
    await withManualTimeoutClock(async (timers) => {
      let nativeReviews = 0;
      const state = harness(
        () => new Promise(() => {}),
        {
          reviewPairing: async () => {
            nativeReviews += 1;
            return "approve";
          },
          confirmCapabilityRevoke: async () => {
            nativeReviews += 1;
            return true;
          },
          reviewDecision: async () => {
            nativeReviews += 1;
            return true;
          },
        },
      );
      const result = scenario.run(state.coordinator);
      await waitForCondition(
        () => state.calls.length === 1 && timers.length === 1,
        `${scenario.name} did not start its bounded presentation request`,
      );

      assert.equal(new URL(state.calls[0].url).pathname, scenario.path);
      assert.equal(timers[0].delay, AGENT_AUTHORITY_REQUEST_TIMEOUT_MS);
      assert.equal(state.calls[0].init.signal.aborted, false);
      timers[0].fire();

      await assert.rejects(
        result,
        (error) => error instanceof DesktopAuthorityError
          && error.code === "authority_request_timeout"
          && error.message === "MemoLens authority request timed out."
          && !error.message.includes("main-policy-secret")
          && !error.message.includes("desktop-session-secret"),
      );
      assert.equal(state.calls[0].init.signal.aborted, true);
      assert.equal(state.calls.length, 1, `${scenario.name} must not submit a mutation after timeout`);
      assert.equal(nativeReviews, 0, `${scenario.name} must not open native review without a presentation`);
      assert.equal(state.trustChecks, 1);
      assert.equal(timers[0].cleared, true);
    });
  }
});

test("authority timeout cancels a stalled response body and releases its timer", async () => {
  await withManualTimeoutClock(async (timers) => {
    let bodyCancellations = 0;
    let responseBody = null;
    const state = harness(() => {
      responseBody = new ReadableStream({
        cancel() {
          bodyCancellations += 1;
        },
      });
      return new Response(responseBody, { status: 200 });
    });
    const result = state.coordinator.reviewPairing(pairingId, projectId);
    await waitForCondition(
      () => state.calls.length === 1 && timers.length === 1,
      "stalled response body did not start its deadline",
    );

    timers[0].fire();
    await assert.rejects(
      result,
      (error) => error instanceof DesktopAuthorityError
        && error.code === "authority_request_timeout",
    );
    await waitForCondition(
      () => bodyCancellations === 1,
      "aborting the authority request did not cancel the response reader",
    );
    assert.equal(state.calls.length, 1);
    assert.equal(timers[0].cleared, true);
    assert.equal(responseBody.locked, false);
  });
});

test("pairing review fetches exact presentation and mutates only after native choice", async () => {
  let nativePresentation = null;
  const state = harness((path, init) => {
    if (path === `/v1/main/agent/pairings/${pairingId}/presentation`) {
      return jsonResponse({
        presentation: {
          pairing_id: pairingId,
          project_id: projectId,
          paired_subject_id: "subject-1",
          claimed_client_label: "Claude (claimed)",
          actions: ["blueprint.commit_proposal", "blueprint.restore_revision"],
          ttl_seconds: 900,
          max_operations: 20,
          observed_head: observedBlueprintHead(),
        },
        presentation_sha256: digest,
        display_code: "ABCD-12",
        expires_at: "2026-08-22T12:15:00Z",
      });
    }
    if (path === `/v1/main/agent/pairings/${pairingId}/approve`) {
      const body = JSON.parse(init.body);
      assert.deepEqual(Object.keys(body).sort(), ["native_gesture_nonce", "presentation_sha256"]);
      assert.equal(body.presentation_sha256, digest);
      assert.match(body.native_gesture_nonce, /^[0-9a-f-]{36}$/);
      return jsonResponse({ status: "completed" });
    }
    if (path === `/v1/creative/projects/${projectId}/blueprint`) return jsonResponse(projection());
    throw new Error(`unexpected path ${path}`);
  }, {
    reviewPairing: async (presentation) => {
      nativePresentation = presentation;
      return "approve";
    },
  });

  const result = await state.coordinator.reviewPairing(pairingId, projectId);

  assert.equal(result.status, "completed");
  assert.equal(state.trustChecks, 2);
  assert.equal(nativePresentation.projectId, projectId);
  assert.equal("presentationDigest" in nativePresentation, false);
  assert.deepEqual(state.calls.map((call) => new URL(call.url).pathname), [
    `/v1/main/agent/pairings/${pairingId}/presentation`,
    `/v1/main/agent/pairings/${pairingId}/approve`,
    `/v1/creative/projects/${projectId}/blueprint`,
  ]);
});

test("pairing native cancellation and cross pairing or project target deny mutation", async () => {
  const mainSource = readFileSync(new URL("../electron/main.ts", import.meta.url), "utf-8");
  const preloadSource = readFileSync(new URL("../electron/preload.cts", import.meta.url), "utf-8");
  assert.match(
    mainSource,
    /authorityCoordinator\.reviewPairing\(pairingId, projectId\)/,
  );
  assert.match(mainSource, /dialog\.showMessageBox[\s\S]*Review Agent pairing/);
  assert.doesNotMatch(preloadSource, /native_gesture_nonce/);

  const preflight = harness(() => {
    throw new Error("malformed pairing scope reached the backend");
  });
  await assert.rejects(
    preflight.coordinator.reviewPairing("pairing/other", projectId),
    (error) => error instanceof DesktopAuthorityError
      && error.code === "invalid_authority_request",
  );
  await assert.rejects(
    preflight.coordinator.reviewPairing(pairingId, "project/other"),
    (error) => error instanceof DesktopAuthorityError
      && error.code === "invalid_authority_request",
  );
  assert.equal(preflight.calls.length, 0);
  assert.equal(preflight.trustChecks, 0);

  const scenarios = [
    {
      name: "native cancellation",
      presentationPairingId: pairingId,
      presentationProjectId: projectId,
      nativeChoice: "cancel",
      expectedNativeReviews: 1,
      expectedStatus: "cancelled",
    },
    {
      name: "cross pairing",
      presentationPairingId: "pairing-other",
      presentationProjectId: projectId,
      nativeChoice: "approve",
      expectedNativeReviews: 0,
      expectedStatus: null,
    },
    {
      name: "cross project",
      presentationPairingId: pairingId,
      presentationProjectId: "project-other",
      nativeChoice: "approve",
      expectedNativeReviews: 0,
      expectedStatus: null,
    },
  ];

  for (const scenario of scenarios) {
    let nativeReviews = 0;
    const state = harness((path) => {
      assert.equal(
        path,
        `/v1/main/agent/pairings/${pairingId}/presentation`,
        `${scenario.name} reached a mutation endpoint`,
      );
      return jsonResponse({
        presentation: {
          pairing_id: scenario.presentationPairingId,
          project_id: scenario.presentationProjectId,
          paired_subject_id: "subject-1",
          claimed_client_label: "Codex (claimed)",
          actions: ["blueprint.commit_proposal"],
          ttl_seconds: 900,
          max_operations: 20,
          observed_head: {
            project_id: scenario.presentationProjectId,
            revision: 3,
            content_sha256: digest,
            semantic_sha256: digest,
            operation_id: "blueprint-operation-3",
          },
        },
        presentation_sha256: digest,
        display_code: "ABCD-12",
        expires_at: "2026-08-22T12:15:00Z",
      });
    }, {
      reviewPairing: async () => {
        nativeReviews += 1;
        return scenario.nativeChoice;
      },
    });

    if (scenario.expectedStatus === null) {
      await assert.rejects(
        state.coordinator.reviewPairing(pairingId, projectId),
        (error) => error instanceof DesktopAuthorityError
          && error.code === "invalid_authority_response",
      );
    } else {
      const result = await state.coordinator.reviewPairing(pairingId, projectId);
      assert.equal(result.status, scenario.expectedStatus);
    }
    assert.equal(nativeReviews, scenario.expectedNativeReviews);
    assert.equal(state.calls.length, 1, `${scenario.name} submitted an authority mutation`);
    assert.equal(state.trustChecks, 1);
  }
});

test("Timeline edit, structural edit, restore, and preview pairing display one exact Timeline head", async () => {
  let nativePresentation = null;
  const state = harness((path) => {
    if (path === `/v1/main/agent/pairings/${pairingId}/presentation`) {
      return jsonResponse({
        presentation: {
          pairing_id: pairingId,
          project_id: projectId,
          paired_subject_id: "subject-1",
          claimed_client_label: "DeepSeek Harness (claimed)",
          actions: [
            "blueprint.commit_proposal",
            "timeline.apply_edit",
            "timeline.apply_structural_edit",
            "timeline.restore_revision",
            "timeline.preview_media",
          ],
          ttl_seconds: 900,
          max_operations: 20,
          observed_head: observedBlueprintHead(),
          observed_timeline_head: observedTimelineHead(),
        },
        presentation_sha256: digest,
        display_code: "EFGH-34",
        expires_at: "2026-08-22T12:15:00Z",
      });
    }
    throw new Error(`a cancelled native review must not mutate authority: ${path}`);
  }, {
    reviewPairing: async (presentation) => {
      nativePresentation = presentation;
      return "cancel";
    },
  });

  const result = await state.coordinator.reviewPairing(pairingId, projectId);

  assert.equal(result.status, "cancelled");
  assert.deepEqual(nativePresentation.requestedActions, [
    "blueprint.commit_proposal",
    "timeline.apply_edit",
    "timeline.apply_structural_edit",
    "timeline.restore_revision",
    "timeline.preview_media",
  ]);
  assert.deepEqual(nativePresentation.observedTimelineHead, {
    timelineId: "timeline-1",
    revision: 2,
    revisionSha256: digest,
    timelineContentSha256: digest,
    operationId: "timeline-operation-2",
    blueprintRevision: 3,
    coverageRevision: 4,
  });
  assert.equal(JSON.stringify(nativePresentation).includes("pairing secret"), false);
  assert.equal(state.calls.length, 1);
});

test("Timeline pairing/head presence is an exact action-bound contract", async () => {
  const scenarios = [
    {
      actions: ["timeline.apply_edit"],
      timelineHead: undefined,
    },
    {
      actions: ["timeline.apply_structural_edit"],
      timelineHead: undefined,
    },
    {
      actions: ["timeline.restore_revision"],
      timelineHead: undefined,
    },
    {
      actions: ["timeline.preview_media"],
      timelineHead: undefined,
    },
    {
      actions: ["blueprint.commit_proposal"],
      timelineHead: observedTimelineHead(),
    },
    {
      actions: ["timeline.apply_edit"],
      timelineHead: {
        ...observedTimelineHead(),
        blueprint_binding: {
          ...observedTimelineHead().blueprint_binding,
          content_sha256: "cd".repeat(32),
        },
      },
    },
  ];

  for (const scenario of scenarios) {
    let nativeReviews = 0;
    const state = harness((path) => {
      if (path !== `/v1/main/agent/pairings/${pairingId}/presentation`) {
        throw new Error(`unexpected path ${path}`);
      }
      const presentation = {
        pairing_id: pairingId,
        project_id: projectId,
        paired_subject_id: "subject-1",
        claimed_client_label: "Codex (claimed)",
        actions: scenario.actions,
        ttl_seconds: 900,
        max_operations: 20,
        observed_head: observedBlueprintHead(),
      };
      if (scenario.timelineHead !== undefined) {
        presentation.observed_timeline_head = scenario.timelineHead;
      }
      return jsonResponse({
        presentation,
        presentation_sha256: digest,
        display_code: "IJKL-56",
        expires_at: "2026-08-22T12:15:00Z",
      });
    }, {
      reviewPairing: async () => {
        nativeReviews += 1;
        return "approve";
      },
    });

    await assert.rejects(
      state.coordinator.reviewPairing(pairingId, projectId),
      (error) => error instanceof DesktopAuthorityError
        && error.code === "invalid_authority_response",
    );
    assert.equal(nativeReviews, 0);
    assert.equal(state.calls.length, 1);
  }
});

test("decision review keeps digest out of presenter and rechecks trust after the dialog", async () => {
  let nativePresentation = null;
  const state = harness((path, init) => {
    if (path === `/v1/main/creative/projects/${projectId}/blueprint/authority/presentation`) {
      assert.deepEqual(JSON.parse(init.body), {
        operation: "confirm",
        decision_units: ["intent_stance", "script"],
      });
      return jsonResponse({
        presentation: {
          project_id: projectId,
          authority_operation: "confirm",
          observed_head: { revision: 3, content_sha256: digest },
          expires_at: "2026-08-22T12:02:00Z",
          decision_units: [
            { name: "intent_stance", content: { stance: "A clear personal stance" }, semantic_sha256: digest },
            { name: "script", content: [{ text: "Three short spoken sections" }], semantic_sha256: digest },
          ],
        },
        presentation_sha256: digest,
      });
    }
    if (path === `/v1/main/creative/projects/${projectId}/blueprint/authority/confirm`) {
      const body = JSON.parse(init.body);
      assert.equal(body.presentation_sha256, digest);
      assert.equal(body.presentation.project_id, projectId);
      assert.match(body.native_gesture_nonce, /^[0-9a-f-]{36}$/);
      assert.match(new Headers(init.headers).get("Idempotency-Key"), /^authority-[0-9a-f-]{36}$/);
      const commandResult = projection(["intent_stance", "script"]);
      delete commandResult.revision;
      return jsonResponse(commandResult);
    }
    throw new Error(`unexpected path ${path}`);
  }, {
    reviewDecision: async (presentation) => {
      nativePresentation = presentation;
      return true;
    },
  });

  const result = await state.coordinator.requestDecisionReview({
    projectId,
    operation: "confirm",
    decisionUnits: ["intent_stance", "script"],
  });

  assert.equal(result.status, "completed");
  assert.equal(result.projectAuthority.confirmedDecisionUnitCount, 2);
  assert.equal(state.trustChecks, 2);
  assert.equal("presentationDigest" in nativePresentation, false);
  assert.equal(JSON.stringify(nativePresentation).includes(digest), false);
});

test("decision backend trust loss after native approval submits zero authority mutation", async () => {
  const state = harness((path) => {
    if (path === `/v1/main/creative/projects/${projectId}/blueprint/authority/presentation`) {
      return jsonResponse({
        presentation: {
          project_id: projectId,
          authority_operation: "confirm",
          observed_head: { revision: 3, content_sha256: digest },
          decision_units: [{
            name: "script",
            content: { text: "exact reviewed script" },
            semantic_sha256: digest,
          }],
        },
        presentation_sha256: digest,
      });
    }
    throw new Error(`authority mutation must not be submitted after trust loss: ${path}`);
  }, {
    reviewDecision: async () => true,
    ensureBackendTrusted: async (_forceIdentityProof, trustCheck) => {
      if (trustCheck === 2) {
        throw new DesktopAuthorityError(
          "native_confirmation_required",
          "MemoLens could not re-verify the local authority service.",
        );
      }
    },
  });

  await assert.rejects(
    state.coordinator.requestDecisionReview({
      projectId,
      operation: "confirm",
      decisionUnits: ["script"],
    }),
    (error) => error instanceof DesktopAuthorityError
      && error.code === "native_confirmation_required",
  );
  assert.equal(state.trustChecks, 2);
  assert.deepEqual(state.calls.map((call) => new URL(call.url).pathname), [
    `/v1/main/creative/projects/${projectId}/blueprint/authority/presentation`,
  ]);
});

test("decision unit duplicates and non-canonical order fail before any fetch", async () => {
  const state = harness(() => {
    throw new Error("network must not be called");
  });
  await assert.rejects(
    state.coordinator.requestDecisionReview({
      projectId,
      operation: "confirm",
      decisionUnits: ["script", "intent_stance"],
    }),
    (error) => error instanceof DesktopAuthorityError
      && error.code === "invalid_blueprint_decision_units",
  );
  assert.equal(state.calls.length, 0);
  assert.equal(state.trustChecks, 0);

  await assert.rejects(
    state.coordinator.requestDecisionReview({
      projectId,
      operation: "confirm",
      decisionUnits: ["script"],
      presentation_sha256: digest,
    }),
    (error) => error instanceof DesktopAuthorityError
      && error.code === "invalid_authority_request",
  );
  assert.equal(state.calls.length, 0);
});

test("decision presentation rejects cross project or malformed Blueprint head before native review", async () => {
  const scenarios = [
    {
      name: "cross project",
      project: "project-other",
      head: { revision: 3, content_sha256: digest },
    },
    {
      name: "missing head digest",
      project: projectId,
      head: { revision: 3 },
    },
    {
      name: "invalid head digest",
      project: projectId,
      head: { revision: 3, content_sha256: "not-a-digest" },
    },
    {
      name: "extra head field",
      project: projectId,
      head: { revision: 3, content_sha256: digest, project_id: projectId },
    },
  ];

  for (const scenario of scenarios) {
    let nativeReviews = 0;
    const state = harness((path) => {
      assert.equal(
        path,
        `/v1/main/creative/projects/${projectId}/blueprint/authority/presentation`,
        `${scenario.name} reached an unexpected endpoint`,
      );
      return jsonResponse({
        presentation: {
          project_id: scenario.project,
          authority_operation: "confirm",
          observed_head: scenario.head,
          decision_units: [{
            name: "script",
            content: { text: "exact reviewed script" },
            semantic_sha256: digest,
          }],
        },
        presentation_sha256: digest,
      });
    }, {
      reviewDecision: async () => {
        nativeReviews += 1;
        return true;
      },
    });

    await assert.rejects(
      state.coordinator.requestDecisionReview({
        projectId,
        operation: "confirm",
        decisionUnits: ["script"],
      }),
      (error) => error instanceof DesktopAuthorityError
        && error.code === "invalid_authority_response",
      scenario.name,
    );
    assert.equal(nativeReviews, 0, scenario.name);
    assert.equal(state.calls.length, 1, scenario.name);
    assert.equal(state.trustChecks, 1, scenario.name);
  }
});

test("capability revoke is bound to a main-fetched presentation", async () => {
  const capability = listPayloads().capabilities.capabilities[0];
  capability.actions = ["timeline.apply_structural_edit"];
  const durableCapability = {
    ...capability,
    id: capability.capability_id,
    operations_used: capability.used_operations,
  };
  delete durableCapability.capability_id;
  delete durableCapability.used_operations;
  let displayed = null;
  const state = harness((path, init) => {
    if (path === `/v1/main/agent/capabilities/${capabilityId}/revoke/presentation`) {
      return jsonResponse({
        presentation: {
          object: "memolens.agent_capability_revocation_presentation",
          schema_version: "1",
          capability: durableCapability,
        },
        presentation_sha256: digest,
      });
    }
    if (path === `/v1/main/agent/capabilities/${capabilityId}/revoke`) {
      const body = JSON.parse(init.body);
      assert.equal(body.presentation.capability.id, capabilityId);
      assert.equal(body.presentation_sha256, digest);
      assert.match(body.native_gesture_nonce, /^[0-9a-f-]{36}$/);
      return jsonResponse({ ...capability, status: "revoked" });
    }
    if (path === `/v1/creative/projects/${projectId}/blueprint`) return jsonResponse(projection());
    throw new Error(`unexpected path ${path}`);
  }, {
    confirmCapabilityRevoke: async (value) => {
      displayed = value;
      return true;
    },
  });

  const result = await state.coordinator.revokeCapability(capabilityId, projectId);

  assert.equal(result.status, "completed");
  assert.equal(displayed.capabilityId, capabilityId);
  assert.deepEqual(displayed.actions, ["timeline.apply_structural_edit"]);
  assert.equal("presentation_sha256" in displayed, false);
  assert.equal(state.trustChecks, 2);
});

test("capability native cancellation and cross capability or project target deny mutation", async () => {
  const mainSource = readFileSync(new URL("../electron/main.ts", import.meta.url), "utf-8");
  const preloadSource = readFileSync(new URL("../electron/preload.cts", import.meta.url), "utf-8");
  assert.match(
    mainSource,
    /authorityCoordinator\.revokeCapability\(capabilityId, projectId\)/,
  );
  assert.match(mainSource, /dialog\.showMessageBox[\s\S]*Revoke Agent access/);
  assert.doesNotMatch(preloadSource, /native_gesture_nonce/);

  const preflight = harness(() => {
    throw new Error("malformed capability scope reached the backend");
  });
  await assert.rejects(
    preflight.coordinator.revokeCapability("capability/other", projectId),
    (error) => error instanceof DesktopAuthorityError
      && error.code === "invalid_authority_request",
  );
  await assert.rejects(
    preflight.coordinator.revokeCapability(capabilityId, "project/other"),
    (error) => error instanceof DesktopAuthorityError
      && error.code === "invalid_authority_request",
  );
  assert.equal(preflight.calls.length, 0);
  assert.equal(preflight.trustChecks, 0);

  const listed = listPayloads().capabilities.capabilities[0];
  const scenarios = [
    {
      name: "native cancellation",
      presentationCapabilityId: capabilityId,
      presentationProjectId: projectId,
      nativeChoice: false,
      expectedNativeReviews: 1,
      expectedStatus: "cancelled",
    },
    {
      name: "cross capability",
      presentationCapabilityId: "capability-other",
      presentationProjectId: projectId,
      nativeChoice: true,
      expectedNativeReviews: 0,
      expectedStatus: null,
    },
    {
      name: "cross project",
      presentationCapabilityId: capabilityId,
      presentationProjectId: "project-other",
      nativeChoice: true,
      expectedNativeReviews: 0,
      expectedStatus: null,
    },
  ];

  for (const scenario of scenarios) {
    let nativeReviews = 0;
    const state = harness((path) => {
      assert.equal(
        path,
        `/v1/main/agent/capabilities/${capabilityId}/revoke/presentation`,
        `${scenario.name} reached a mutation endpoint`,
      );
      const capability = {
        ...listed,
        id: scenario.presentationCapabilityId,
        project_id: scenario.presentationProjectId,
        operations_used: listed.used_operations,
      };
      delete capability.capability_id;
      delete capability.used_operations;
      return jsonResponse({
        presentation: {
          object: "memolens.agent_capability_revocation_presentation",
          schema_version: "1",
          capability,
        },
        presentation_sha256: digest,
      });
    }, {
      confirmCapabilityRevoke: async () => {
        nativeReviews += 1;
        return scenario.nativeChoice;
      },
    });

    if (scenario.expectedStatus === null) {
      await assert.rejects(
        state.coordinator.revokeCapability(capabilityId, projectId),
        (error) => error instanceof DesktopAuthorityError
          && error.code === "agent_pairing_revoked",
      );
    } else {
      const result = await state.coordinator.revokeCapability(capabilityId, projectId);
      assert.equal(result.status, scenario.expectedStatus);
    }
    assert.equal(nativeReviews, scenario.expectedNativeReviews);
    assert.equal(state.calls.length, 1, `${scenario.name} submitted a capability mutation`);
    assert.equal(state.trustChecks, 1);
  }
});

test("native display sanitizer removes bidi, isolate, and zero-width format controls", () => {
  const spoofed = "Claude\u202Ecod.exe\u202C\u2066 hidden\u2069\u200B\u061C\uFEFF";
  const cleaned = sanitizeNativeAuthorityText(spoofed);
  assert.equal(cleaned, "Claudecod.exe hidden");
  assert.doesNotMatch(cleaned, /[\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]/);
});

test("native decision content is complete, lossless, control-safe, and tail-distinguishing", () => {
  const sharedPrefix = "A".repeat(3_000);
  const rawA = {
    nested: { script: `${sharedPrefix}VISIBLE-TAIL-A\u202E` },
    sections: Array.from({ length: 14 }, (_, index) => ({ index, text: `section-${index}` })),
  };
  const rawB = {
    nested: { script: `${sharedPrefix}VISIBLE-TAIL-B\u202E` },
    sections: Array.from({ length: 14 }, (_, index) => ({ index, text: `section-${index}` })),
  };

  const canonicalA = canonicalizeNativeDecisionContent(rawA);
  const canonicalB = canonicalizeNativeDecisionContent(rawB);
  const pagesA = paginateNativeDecisionContent(canonicalA);
  const pagesB = paginateNativeDecisionContent(canonicalB);

  assert.deepEqual(JSON.parse(canonicalA), rawA);
  assert.deepEqual(JSON.parse(canonicalB), rawB);
  assert.equal(pagesA.join(""), canonicalA);
  assert.equal(pagesB.join(""), canonicalB);
  assert.notDeepEqual(pagesA, pagesB);
  assert.match(canonicalA, /VISIBLE-TAIL-A\\u202e/);
  assert.match(canonicalB, /VISIBLE-TAIL-B\\u202e/);
  assert.doesNotMatch(canonicalA, /\u202e/);
  assert.ok(canonicalA.length > 2_000, "the full content must not be truncated to an old summary cap");
});

test("native decision review uses the frozen one-MiB authority value budget", () => {
  const aboveLegacyReaderLimit = { script: `start-${"A".repeat(300 * 1024)}-tail` };
  const canonical = canonicalizeNativeDecisionContent(aboveLegacyReaderLimit);
  assert.deepEqual(JSON.parse(canonical), aboveLegacyReaderLimit);
  assert.match(canonical, /-tail"\s*}/);

  const aboveSemanticBudgetButWithinPresentationBudget = {
    script: `start-${"B".repeat(600 * 1024)}-tail`,
  };
  assert.deepEqual(
    JSON.parse(canonicalizeNativeDecisionContent(aboveSemanticBudgetButWithinPresentationBudget)),
    aboveSemanticBudgetButWithinPresentationBudget,
  );

  assert.throws(
    () => canonicalizeNativeDecisionContent({ script: "C".repeat(1024 * 1024) }),
    (error) => error instanceof DesktopAuthorityError
      && error.code === "invalid_authority_response",
  );
});

test("authority response reader accepts a legal Blueprint envelope above one MiB", async () => {
  const payloads = listPayloads();
  const largeProjection = projection(["script"]);
  largeProjection.blueprint_transport_padding = "P".repeat(1_100_000);
  const state = harness((path) => {
    if (path === "/v1/main/agent/pairings") return jsonResponse(payloads.pairings);
    if (path === "/v1/main/agent/capabilities") return jsonResponse(payloads.capabilities);
    if (path === `/v1/creative/projects/${projectId}/blueprint`) {
      return jsonResponse(largeProjection);
    }
    throw new Error(`unexpected path ${path}`);
  });

  const result = await state.coordinator.list(projectId);
  assert.equal(result.projectAuthority.confirmedDecisionUnitCount, 1);
});

test("authority response length is rejected before an oversized body is consumed", async () => {
  let bodyCancellations = 0;
  let oversizedBody = null;
  const state = harness((path) => {
    if (path === "/v1/main/agent/pairings") {
      oversizedBody = new ReadableStream({
        cancel() {
          bodyCancellations += 1;
        },
      });
      return new Response(oversizedBody, {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "Content-Length": String(2 * 1024 * 1024 + 1),
        },
      });
    }
    if (path === "/v1/main/agent/capabilities") {
      return jsonResponse({ capabilities: [] });
    }
    throw new Error(`unexpected path ${path}`);
  });

  await assert.rejects(
    state.coordinator.list(projectId),
    (error) => error instanceof DesktopAuthorityError
      && error.code === "invalid_authority_response",
  );
  assert.equal(state.calls.length, 2);
  assert.equal(bodyCancellations, 1);
  assert.equal(oversizedBody.locked, false);
});

test("native decision review requires every content page and a separate final gesture", async () => {
  const content = canonicalizeNativeDecisionContent({ script: "full script ".repeat(500) });
  const presentation = {
    projectId,
    operation: "confirm",
    observedRevision: 3,
    decisionUnits: [{ name: "script", label: "Script", content }],
  };
  const expectedPages = paginateNativeDecisionContent(content);
  const acceptedSteps = [];
  const accepted = await conductNativeDecisionReview(presentation, async (step) => {
    acceptedSteps.push(step);
    return true;
  });

  assert.equal(accepted, true);
  assert.equal(acceptedSteps.at(-1).kind, "confirmation");
  const shownPages = acceptedSteps.filter((step) => step.kind === "content");
  assert.equal(shownPages.length, expectedPages.length);
  assert.equal(shownPages.map((step) => step.pageContent).join(""), content);
  assert.deepEqual(shownPages.map((step) => step.pageIndex), expectedPages.map((_, index) => index));

  const cancelledSteps = [];
  const cancelled = await conductNativeDecisionReview(presentation, async (step) => {
    cancelledSteps.push(step);
    return cancelledSteps.length < 2;
  });
  assert.equal(cancelled, false);
  assert.equal(cancelledSteps.length, 2);
  assert.equal(cancelledSteps.some((step) => step.kind === "confirmation"), false);
});

test("cancelling the distinct final gesture submits no decision authority command", async () => {
  const visitedSteps = [];
  const state = harness((path) => {
    if (path === `/v1/main/creative/projects/${projectId}/blueprint/authority/presentation`) {
      return jsonResponse({
        presentation: {
          project_id: projectId,
          authority_operation: "confirm",
          observed_head: { revision: 3, content_sha256: digest },
          decision_units: [{
            name: "script",
            content: { text: `prefix-${"X".repeat(3_000)}-critical-tail` },
            semantic_sha256: digest,
          }],
        },
        presentation_sha256: digest,
      });
    }
    if (path === `/v1/creative/projects/${projectId}/blueprint`) {
      return jsonResponse(projection());
    }
    throw new Error(`authority mutation must not be called after cancellation: ${path}`);
  }, {
    reviewDecision: (presentation) => conductNativeDecisionReview(
      presentation,
      async (step) => {
        visitedSteps.push(step);
        return step.kind === "content";
      },
    ),
  });

  const result = await state.coordinator.requestDecisionReview({
    projectId,
    operation: "confirm",
    decisionUnits: ["script"],
  });

  assert.equal(result.status, "cancelled");
  assert.equal(visitedSteps.at(-1).kind, "confirmation");
  assert.match(
    visitedSteps.filter((step) => step.kind === "content").map((step) => step.pageContent).join(""),
    /critical-tail/,
  );
  assert.deepEqual(state.calls.map((call) => new URL(call.url).pathname), [
    `/v1/main/creative/projects/${projectId}/blueprint/authority/presentation`,
    `/v1/creative/projects/${projectId}/blueprint`,
  ]);
});

test("authority projection rejects contradictory verified claims", async () => {
  const payloads = listPayloads();
  const malformed = projection();
  malformed.authority_projection.decision_units.script.verified = true;
  const state = harness((path) => {
    if (path === "/v1/main/agent/pairings") return jsonResponse(payloads.pairings);
    if (path === "/v1/main/agent/capabilities") return jsonResponse(payloads.capabilities);
    if (path === `/v1/creative/projects/${projectId}/blueprint`) return jsonResponse(malformed);
    throw new Error(`unexpected path ${path}`);
  });
  await assert.rejects(
    state.coordinator.list(projectId),
    (error) => error instanceof DesktopAuthorityError
      && error.code === "invalid_authority_response",
  );
});

test("decision command response fails closed instead of hiding a malformed projection", async () => {
  const state = harness((path) => {
    if (path === `/v1/main/creative/projects/${projectId}/blueprint/authority/presentation`) {
      return jsonResponse({
        presentation: {
          project_id: projectId,
          authority_operation: "confirm",
          observed_head: { revision: 3, content_sha256: digest },
          decision_units: [{ name: "script", content: { text: "Exact script" }, semantic_sha256: digest }],
        },
        presentation_sha256: digest,
      });
    }
    if (path === `/v1/main/creative/projects/${projectId}/blueprint/authority/confirm`) {
      const malformed = projection(["script"]);
      malformed.creation_authority.verified = true;
      return jsonResponse(malformed);
    }
    throw new Error(`unexpected fallback path ${path}`);
  }, {
    reviewDecision: async () => true,
  });

  await assert.rejects(
    state.coordinator.requestDecisionReview({
      projectId,
      operation: "confirm",
      decisionUnits: ["script"],
    }),
    (error) => error instanceof DesktopAuthorityError
      && error.code === "invalid_authority_response",
  );
  assert.equal(state.calls.length, 2);
});

test("decision presentation must contain exactly the requested unit set", async () => {
  const state = harness((path) => {
    if (path === `/v1/main/creative/projects/${projectId}/blueprint/authority/presentation`) {
      return jsonResponse({
        presentation: {
          project_id: projectId,
          authority_operation: "confirm",
          observed_head: { revision: 3, content_sha256: digest },
          decision_units: [{ name: "intent_stance", content: { stance: "Only one unit" }, semantic_sha256: digest }],
        },
        presentation_sha256: digest,
      });
    }
    throw new Error(`unexpected path ${path}`);
  }, {
    reviewDecision: async () => {
      throw new Error("native review must not open");
    },
  });

  await assert.rejects(
    state.coordinator.requestDecisionReview({
      projectId,
      operation: "confirm",
      decisionUnits: ["intent_stance", "script"],
    }),
    (error) => error instanceof DesktopAuthorityError
      && error.code === "invalid_authority_response",
  );
  assert.equal(state.calls.length, 1);
});

test("backend trust loss rotates desktop and main credentials plus runtime epoch together", () => {
  const before = {
    desktop: getDesktopSessionToken(),
    main: getMainAuthorityToken(),
    epoch: getRuntimeAuthorityEpoch(),
  };
  revokeBackendTrust();
  assert.notEqual(getDesktopSessionToken(), before.desktop);
  assert.notEqual(getMainAuthorityToken(), before.main);
  assert.notEqual(getRuntimeAuthorityEpoch(), before.epoch);
});

test("managed backend receives main authority and runtime epoch only through spawn env", async () => {
  class FakeChildProcess extends EventEmitter {
    stdout = new EventEmitter();
    stderr = new EventEmitter();
    exitCode = null;
    kill() { return true; }
  }
  const spawns = [];
  const logs = [];
  let spawnedProcess = null;
  let runtimeCheck = null;
  let probes = 0;
  const supervisor = new BackendProcessSupervisor({
    backendUrl: "http://127.0.0.1:5519",
    probeHealth: async () => {
      probes += 1;
      return probes > 1;
    },
    getSessionToken: () => "desktop-token",
    getMainAuthorityToken: () => "main-token",
    getRuntimeAuthorityEpoch: () => "epoch-1",
    rotateSessionToken() {},
    updateBackendTrust() {},
    getAppStateDir: () => "/memo-state",
    execFileProcess: (command, args, options, callback) => {
      runtimeCheck = { command, args, options };
      callback(null, JSON.stringify({
        object: "memolens.sqlite_runtime",
        schema_version: "1",
        policy_id: "sqlite-wal-reset-2026-08-22-v1",
        python_version: "3.14.2",
        sqlite_version: "3.53.4",
        wal_reset_safe: true,
        journal_policy: "wal",
        status: "ready",
        reason_code: null,
      }), "");
    },
    spawnProcess: (command, args, options) => {
      spawns.push({ command, args, options });
      spawnedProcess = new FakeChildProcess();
      return spawnedProcess;
    },
    resolveExecutableIdentity: (command) => ({
      realpath: command,
      device: "1",
      inode: "1",
      size: "4096",
      mtimeNs: "1",
    }),
    readEnvFile: () => { throw new Error("missing"); },
    environment: () => ({ OPENAI_API_KEY: "provider-secret" }),
    logger: {
      log: (message) => logs.push(message),
      error: (message) => logs.push(message),
    },
  });
  const result = await supervisor.ensureReady("/project", {
    pythonCommand: "python3",
    autoStartBackend: true,
    libraryConfigured: false,
    defaultLibraryDir: null,
    defaultDbPath: null,
  });
  assert.equal(result.state, "started");
  assert.equal(runtimeCheck.options.env.MEMOLENS_DESKTOP_SESSION_TOKEN, undefined);
  assert.equal(runtimeCheck.options.env.MEMOLENS_MAIN_AUTHORITY_TOKEN, undefined);
  assert.equal(runtimeCheck.options.env.MEMOLENS_RUNTIME_AUTHORITY_EPOCH, undefined);
  assert.equal(runtimeCheck.options.env.OPENAI_API_KEY, undefined);
  assert.equal(spawns[0].command, managedPythonCommand("/project"));
  assert.deepEqual(spawns[0].args, ["-I", "-u", "backend/app.py"]);
  assert.equal(spawns[0].options.env.MEMOLENS_DESKTOP_SESSION_TOKEN, "desktop-token");
  assert.equal(spawns[0].options.env.MEMOLENS_MAIN_AUTHORITY_TOKEN, "main-token");
  assert.equal(spawns[0].options.env.MEMOLENS_RUNTIME_AUTHORITY_EPOCH, "epoch-1");
  spawnedProcess.stdout.emit("data", Buffer.from("desktop-token main-token epoch-1\n"));
  spawnedProcess.stderr.emit("data", Buffer.from("main-token\n"));
  spawnedProcess.stderr.emit("data", Buffer.from("provider-secret\n"));

  for (const secret of ["desktop-token", "main-token", "epoch-1", "provider-secret"]) {
    for (let split = 1; split < secret.length; split += 1) {
      spawnedProcess.stdout.emit("data", Buffer.from(`stdout-${split}:${secret.slice(0, split)}`));
      spawnedProcess.stdout.emit("data", Buffer.from(`${secret.slice(split)}\n`));
      spawnedProcess.stderr.emit("data", Buffer.from(`stderr-${split}:${secret.slice(0, split)}`));
      spawnedProcess.stderr.emit("data", Buffer.from(`${secret.slice(split)}\n`));
    }
  }
  spawnedProcess.stdout.emit("data", Buffer.from("desktop-"));
  spawnedProcess.stdout.emit("data", Buffer.from("tokenmain-tok"));
  spawnedProcess.stdout.emit("data", Buffer.from("enepoch-1\n"));
  spawnedProcess.stderr.emit("data", Buffer.from("main-"));
  spawnedProcess.stderr.emit("data", Buffer.from("tok"));
  spawnedProcess.stderr.emit("data", Buffer.from("enmain-token\n"));
  spawnedProcess.stdout.emit("data", Buffer.from("ordinary trailing mai"));
  spawnedProcess.stdout.emit("end");
  spawnedProcess.stderr.emit("end");

  const combinedLogs = logs.join("\n");
  assert.equal(/desktop-token|main-token|epoch-1|provider-secret/.test(combinedLogs), false);
  assert.equal(logs.some((message) => message.includes("[redacted]")), true);
  assert.equal(logs.some((message) => message.endsWith("mai")), true);
});

test("managed backend refuses a half-configured authority runtime", () => {
  assert.throws(() => new BackendProcessSupervisor({
    backendUrl: "http://127.0.0.1:5519",
    probeHealth: async () => false,
    getSessionToken: () => "desktop-token",
    getMainAuthorityToken: () => "main-token",
    rotateSessionToken() {},
    updateBackendTrust() {},
    getAppStateDir: () => "/memo-state",
  }), /configured together/);
});

test("preload exposes review verbs but never the main credential or issuer primitives", () => {
  const preload = readFileSync("electron/preload.cts", "utf8");
  assert.match(preload, /listAgentAuthority/);
  assert.match(preload, /reviewAgentPairing/);
  assert.match(preload, /revokeAgentCapability/);
  assert.match(preload, /requestDecisionAuthorityReview/);
  assert.equal(preload.includes("X-MemoLens-Main-Authority"), false);
  assert.equal(preload.includes("getMainAuthorityToken"), false);
  assert.equal(preload.includes("runtimeAuthorityEpoch"), false);
  assert.equal(preload.includes("presentation_sha256"), false);
  assert.equal(preload.includes("approveAgentPairing"), false);
  assert.equal(preload.includes("confirmed: true"), false);

  const mainSource = readFileSync("electron/main.ts", "utf8");
  const injectionStart = mainSource.indexOf("function configureDesktopSessionAuthentication");
  const injectionEnd = mainSource.indexOf("function createWindow");
  const rendererHeaderInjection = mainSource.slice(injectionStart, injectionEnd);
  assert.match(rendererHeaderInjection, /X-MemoLens-Desktop-Token/);
  assert.equal(rendererHeaderInjection.includes("X-MemoLens-Main-Authority"), false);
  assert.match(
    mainSource,
    /"memolens:request-decision-authority-review",[\s\S]*?assertTrustedIpcSender\(event, trustedRendererEntries\);[\s\S]*?runNativeAuthorityReview\(\(\) => \([\s\S]*?authorityCoordinator\.requestDecisionReview\(request\)/,
  );
});
