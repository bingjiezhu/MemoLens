import assert from "node:assert/strict";
import test from "node:test";

import { BackendClient } from "./backendClient.js";
import {
  networkPolicyObservation,
  networkProfile,
  PhotonNetworkPolicyError,
  requireExternalPlatformAllowed,
  requireLocalServiceUrl,
  requireLoopbackUrlAllowed,
  resetNetworkPolicyObservation,
} from "./networkPolicy.js";

const offline = { MEMOLENS_NETWORK_PROFILE: "offline" };

test("offline Photon admits only literal loopback HTTP targets", () => {
  requireLoopbackUrlAllowed("http://127.0.0.1:5519/v1/retrieval/query", offline);
  requireLoopbackUrlAllowed("http://[::1]:5519/v1/retrieval/query", offline);
  for (const target of (
    [
      "https://discord.com/api",
      "http://localhost:5519/v1/retrieval/query",
      "file:///tmp/private",
    ]
  )) {
    assert.throws(
      () => requireLoopbackUrlAllowed(target, offline),
      PhotonNetworkPolicyError,
    );
  }
});

test("local backend policy rejects non-literal and remote hosts in every profile", () => {
  for (const environment of (
    [
      { MEMOLENS_NETWORK_PROFILE: "online" },
      { MEMOLENS_NETWORK_PROFILE: "offline" },
    ]
  )) {
    assert.equal(networkProfile(environment), environment.MEMOLENS_NETWORK_PROFILE);
    requireLocalServiceUrl("https://127.0.0.2:5519/v1/retrieval/query", environment);
    requireLocalServiceUrl("http://[::1]:5519/v1/retrieval/query", environment);
    for (const target of [
      "https://provider.example.invalid/v1/retrieval/query",
      "http://localhost:5519/v1/retrieval/query",
      "http://2130706433:5519/v1/retrieval/query",
      "http://127.1:5519/v1/retrieval/query",
      "http://user:secret@127.0.0.1:5519/v1/retrieval/query",
    ]) {
      assert.throws(
        () => requireLocalServiceUrl(target, environment),
        PhotonNetworkPolicyError,
      );
    }
  }
});

test("offline Photon denies external messaging platforms before use", () => {
  assert.throws(
    () => requireExternalPlatformAllowed("discord", offline),
    /denied an external messaging platform before transmission/,
  );
  assert.throws(
    () => requireExternalPlatformAllowed("imessage", offline),
    /denied an external messaging platform before transmission/,
  );
});

test("BackendClient preflights target before fetch and keeps loopback usable", async () => {
  const previousProfile = process.env.MEMOLENS_NETWORK_PROFILE;
  const previousFetch = globalThis.fetch;
  let fetchCount = 0;
  process.env.MEMOLENS_NETWORK_PROFILE = "offline";
  globalThis.fetch = async (_input, init) => {
    fetchCount += 1;
    assert.equal(init?.redirect, "error");
    assert.deepEqual(JSON.parse(String(init?.body)), {
      text: "fixture",
      top_k: 1,
      include_copy: false,
    });
    return new Response(JSON.stringify({ status: "completed", data: [], notes: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    const baseConfig = {
      requestTimeoutMs: 1_000,
      imageLibraryDir: "/not-transmitted",
      dbPath: null,
      backendSendPathOverrides: false,
    };
    await new BackendClient({
      ...baseConfig,
      backendBaseUrl: "http://127.0.0.1:5519",
    }).queryPhotos({ text: "fixture", topK: 1 });
    assert.equal(fetchCount, 1);

    process.env.MEMOLENS_NETWORK_PROFILE = "online";

    await assert.rejects(
      new BackendClient({
        ...baseConfig,
        backendBaseUrl: "https://provider.example.invalid",
      }).queryPhotos({ text: "fixture", topK: 1 }),
      PhotonNetworkPolicyError,
    );
    assert.equal(fetchCount, 1);
  } finally {
    globalThis.fetch = previousFetch;
    if (previousProfile === undefined) delete process.env.MEMOLENS_NETWORK_PROFILE;
    else process.env.MEMOLENS_NETWORK_PROFILE = previousProfile;
  }
});

test("observation is scoped and contains no target values", () => {
  const previousProfile = process.env.MEMOLENS_NETWORK_PROFILE;
  process.env.MEMOLENS_NETWORK_PROFILE = "offline";
  resetNetworkPolicyObservation();
  try {
    assert.throws(
      () => requireLocalServiceUrl("https://private.example.invalid/secret"),
      PhotonNetworkPolicyError,
    );
    const encoded = JSON.stringify(networkPolicyObservation());
    assert.match(encoded, /photon-node/);
    assert.match(encoded, /"os_packet_capture":false/);
    assert.doesNotMatch(encoded, /private\.example\.invalid|secret/);
  } finally {
    if (previousProfile === undefined) delete process.env.MEMOLENS_NETWORK_PROFILE;
    else process.env.MEMOLENS_NETWORK_PROFILE = previousProfile;
  }
});

test("unknown network profiles fail closed", () => {
  assert.throws(
    () => networkProfile({ MEMOLENS_NETWORK_PROFILE: "future-auto" }),
    PhotonNetworkPolicyError,
  );
  assert.throws(
    () => requireLocalServiceUrl(
      "http://127.0.0.1:5519",
      { MEMOLENS_NETWORK_PROFILE: "future-auto" },
    ),
    PhotonNetworkPolicyError,
  );
});
