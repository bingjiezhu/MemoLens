import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  ELECTRON_NETWORK_POLICY_SCOPE,
  ElectronNetworkPolicyError,
  configureElectronSessionNetworkPolicy,
  createGuardedFetch,
  dispatchExternalNavigation,
  getElectronNetworkPolicyCounters,
  getElectronNetworkPolicyObservation,
  isLiteralLoopbackUrl,
  parseMemoLensNetworkProfile,
  requireElectronNetworkTarget,
  resetElectronNetworkPolicyCountersForTests,
} from "../electron-dist/electron/networkPolicy.js";

function withNetworkProfile(profile, action) {
  const previous = process.env.MEMOLENS_NETWORK_PROFILE;
  if (profile === undefined) {
    delete process.env.MEMOLENS_NETWORK_PROFILE;
  } else {
    process.env.MEMOLENS_NETWORK_PROFILE = profile;
  }
  return Promise.resolve()
    .then(action)
    .finally(() => {
      if (previous === undefined) {
        delete process.env.MEMOLENS_NETWORK_PROFILE;
      } else {
        process.env.MEMOLENS_NETWORK_PROFILE = previous;
      }
    });
}

test("network profile defaults online, normalizes valid input, and fails closed on invalid input", async () => {
  await withNetworkProfile(undefined, () => {
    assert.equal(parseMemoLensNetworkProfile(), "online");
  });
  await withNetworkProfile("  OFFLINE ", () => {
    assert.equal(parseMemoLensNetworkProfile(), "offline");
  });
  for (const invalid of ["", "disabled", "off", "1"]) {
    assert.throws(
      () => parseMemoLensNetworkProfile(invalid),
      (error) => error instanceof ElectronNetworkPolicyError
        && error.code === "invalid_profile",
    );
  }
  assert.throws(
    () => parseMemoLensNetworkProfile("private-profile-secret"),
    (error) => error instanceof ElectronNetworkPolicyError
      && !error.message.includes("private-profile-secret"),
  );
});

test("offline URL admission requires a literal IP loopback without credentials", () => {
  for (const value of [
    "http://127.0.0.1:5519/healthz",
    "https://127.42.3.9:8443/v1/indexing/jobs",
    "http://[::1]:5519/healthz",
  ]) {
    assert.equal(isLiteralLoopbackUrl(value), true, value);
  }
  for (const value of [
    "http://localhost:5519/healthz",
    "http://127.0.0.1.example.test/healthz",
    "http://2130706433:5519/healthz",
    "http://127.1:5519/healthz",
    "http://0x7f000001:5519/healthz",
    "http://0177.0.0.1:5519/healthz",
    "http://127.000.0.1:5519/healthz",
    "https://192.0.2.1/resource",
    "http://user:password@127.0.0.1:5519/healthz",
    "file:///private/library/photo.jpg",
    "not a URL",
  ]) {
    assert.equal(isLiteralLoopbackUrl(value), false, value);
  }
});

test("Electron renderer development URL uses the same literal-loopback admission", () => {
  const mainSource = readFileSync("electron/main.ts", "utf-8");
  assert.match(
    mainSource,
    /requestedDevUrl && isLiteralLoopbackUrl\(requestedDevUrl\)/,
  );
  assert.doesNotMatch(mainSource, /\["127\.0\.0\.1",\s*"localhost"/);
});

test("offline guarded fetch rejects before send and disables redirect following", async () => {
  await withNetworkProfile("offline", async () => {
    resetElectronNetworkPolicyCountersForTests();
    const calls = [];
    const fakeFetch = async (input, init) => {
      calls.push({ input: String(input), init });
      return new Response("ok");
    };
    const guarded = createGuardedFetch(fakeFetch);

    await guarded("http://127.0.0.1:5519/healthz", {
      method: "POST",
      body: "private-payload",
      redirect: "follow",
    });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].init.redirect, "error");

    const secretUrl = "https://example.test/private/library/user.jpg";
    await assert.rejects(
      guarded(secretUrl, { method: "POST", body: "private-payload" }),
      (error) => error instanceof ElectronNetworkPolicyError
        && error.code === "network_denied"
        && !error.message.includes("example.test")
        && !error.message.includes("user.jpg")
        && !error.message.includes("private-payload"),
    );
    assert.equal(calls.length, 1, "remote target must be rejected before the fetch implementation");
    assert.deepEqual(getElectronNetworkPolicyCounters(), {
      allowedOnline: 0,
      allowedLoopback: 1,
      allowedUnixSocket: 0,
      deniedRemote: 1,
      deniedMalformed: 0,
    });
  });
});

test("online guarded fetch preserves caller redirect behavior", async () => {
  await withNetworkProfile("online", async () => {
    resetElectronNetworkPolicyCountersForTests();
    let observedInit;
    const guarded = createGuardedFetch(async (_input, init) => {
      observedInit = init;
      return new Response("ok");
    });
    await guarded("https://example.test/resource", { redirect: "follow" });
    assert.equal(observedInit.redirect, "follow");
    assert.equal(getElectronNetworkPolicyCounters().allowedOnline, 1);
  });
});

test("offline Electron rejects external navigation before dispatch", async () => {
  for (const profile of ["offline", "invalid-profile"]) {
    await withNetworkProfile(profile, async () => {
      resetElectronNetworkPolicyCountersForTests();
      const dispatched = [];
      await assert.rejects(
        dispatchExternalNavigation(
          "codex://plugins/memolens?private=project-path",
          async (targetUrl) => dispatched.push(targetUrl),
        ),
        (error) => error instanceof ElectronNetworkPolicyError
          && (error.code === "network_denied" || error.code === "invalid_profile")
          && !error.message.includes("project-path"),
      );
      assert.deepEqual(dispatched, [], `${profile} reached the OS URL dispatcher`);
    });
  }

  await withNetworkProfile("online", async () => {
    resetElectronNetworkPolicyCountersForTests();
    const dispatched = [];
    await dispatchExternalNavigation(
      "codex://plugins/memolens",
      async (targetUrl) => dispatched.push(targetUrl),
    );
    assert.deepEqual(dispatched, ["codex://plugins/memolens"]);
    assert.equal(getElectronNetworkPolicyCounters().allowedOnline, 1);
  });
});

test("offline admits only absolute Unix socket targets outside URL fetches", () => {
  resetElectronNetworkPolicyCountersForTests();
  requireElectronNetworkTarget(
    { kind: "unix", socketPath: "/tmp/memolens/backend.sock" },
    "offline",
  );
  assert.throws(
    () => requireElectronNetworkTarget(
      { kind: "unix", socketPath: "relative/backend.sock" },
      "offline",
    ),
    (error) => error instanceof ElectronNetworkPolicyError
      && error.code === "malformed_target"
      && !error.message.includes("backend.sock"),
  );
  assert.equal(getElectronNetworkPolicyCounters().allowedUnixSocket, 1);
  assert.equal(getElectronNetworkPolicyCounters().deniedMalformed, 1);
});

test("offline Electron Session bypasses proxies and blocks remote Chromium requests", async () => {
  resetElectronNetworkPolicyCountersForTests();
  const proxyConfigurations = [];
  let requestFilter;
  let listener;
  const fakeSession = {
    async setProxy(configuration) {
      proxyConfigurations.push(configuration);
    },
    webRequest: {
      onBeforeRequest(filter, callback) {
        requestFilter = filter;
        listener = callback;
      },
    },
  };

  assert.equal(await configureElectronSessionNetworkPolicy(fakeSession, "offline"), "offline");
  assert.deepEqual(proxyConfigurations, [{ mode: "direct" }]);
  assert.deepEqual(requestFilter, {
    urls: ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"],
  });

  const decisions = [];
  listener({ url: "ws://[::1]:5519/events" }, (decision) => decisions.push(decision));
  listener(
    { url: "https://example.test/private/library.jpg" },
    (decision) => decisions.push(decision),
  );
  listener({ url: "https://localhost:5519/healthz" }, (decision) => decisions.push(decision));
  assert.deepEqual(decisions, [{ cancel: false }, { cancel: true }, { cancel: true }]);
  assert.equal(getElectronNetworkPolicyCounters().allowedLoopback, 1);
  assert.equal(getElectronNetworkPolicyCounters().deniedRemote, 2);
});

test("online Electron Session installs no proxy or webRequest override", async () => {
  let touched = false;
  const fakeSession = {
    async setProxy() {
      touched = true;
    },
    webRequest: {
      onBeforeRequest() {
        touched = true;
      },
    },
  };
  assert.equal(await configureElectronSessionNetworkPolicy(fakeSession, "online"), "online");
  assert.equal(touched, false);
});

test("observation is aggregate-only and states the enforcement boundary honestly", async () => {
  await withNetworkProfile("offline", () => {
    resetElectronNetworkPolicyCountersForTests();
    const observation = getElectronNetworkPolicyObservation();
    assert.deepEqual(observation.scope, ELECTRON_NETWORK_POLICY_SCOPE);
    assert.equal(observation.scope.appDeclaredFetch, true);
    assert.equal(observation.scope.chromiumSessionRequests, true);
    assert.equal(observation.scope.operatingSystemTraffic, false);
    const serialized = JSON.stringify(observation);
    for (const forbidden of ["host", "path", "payload", "credential", "header", "url"]) {
      assert.equal(serialized.toLowerCase().includes(forbidden), false, forbidden);
    }
  });
});
