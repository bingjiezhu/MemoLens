import { isAbsolute } from "node:path";
import { isIP } from "node:net";

export type MemoLensNetworkProfile = "online" | "offline";

export const ELECTRON_NETWORK_POLICY_SCOPE = Object.freeze({
  appDeclaredFetch: true,
  chromiumSessionRequests: true,
  operatingSystemTraffic: false,
});

export interface ElectronNetworkPolicyCounters {
  allowedOnline: number;
  allowedLoopback: number;
  allowedUnixSocket: number;
  deniedRemote: number;
  deniedMalformed: number;
}

type NetworkCounterName = keyof ElectronNetworkPolicyCounters;

const counters: ElectronNetworkPolicyCounters = {
  allowedOnline: 0,
  allowedLoopback: 0,
  allowedUnixSocket: 0,
  deniedRemote: 0,
  deniedMalformed: 0,
};

export class ElectronNetworkPolicyError extends Error {
  readonly code: "invalid_profile" | "network_denied" | "malformed_target";

  constructor(
    code: "invalid_profile" | "network_denied" | "malformed_target",
    message: string,
  ) {
    super(message);
    this.name = "ElectronNetworkPolicyError";
    this.code = code;
  }
}

function increment(counter: NetworkCounterName): void {
  counters[counter] += 1;
}

/**
 * Returns aggregate decisions only. Target hosts, URLs, paths, headers, and
 * payloads are deliberately absent from this diagnostic surface.
 */
export function getElectronNetworkPolicyCounters(): ElectronNetworkPolicyCounters {
  return { ...counters };
}

export function getElectronNetworkPolicyObservation(): {
  object: "memolens.electron_network_policy_observation";
  schemaVersion: "1";
  profile: MemoLensNetworkProfile;
  scope: typeof ELECTRON_NETWORK_POLICY_SCOPE;
  counters: ElectronNetworkPolicyCounters;
} {
  return {
    object: "memolens.electron_network_policy_observation",
    schemaVersion: "1",
    profile: parseMemoLensNetworkProfile(),
    scope: ELECTRON_NETWORK_POLICY_SCOPE,
    counters: getElectronNetworkPolicyCounters(),
  };
}

export function resetElectronNetworkPolicyCountersForTests(): void {
  for (const counter of Object.keys(counters) as NetworkCounterName[]) {
    counters[counter] = 0;
  }
}

export function parseMemoLensNetworkProfile(
  rawProfile: string | undefined = process.env.MEMOLENS_NETWORK_PROFILE,
): MemoLensNetworkProfile {
  const normalized = rawProfile === undefined
    ? "online"
    : rawProfile.trim().toLowerCase();
  if (normalized === "online" || normalized === "offline") {
    return normalized;
  }
  throw new ElectronNetworkPolicyError(
    "invalid_profile",
    "MEMOLENS_NETWORK_PROFILE must be either online or offline.",
  );
}

function isLiteralIpv4Loopback(hostname: string): boolean {
  const octets = hostname.split(".");
  if (octets.length !== 4 || octets.some((octet) => !/^(?:0|[1-9][0-9]{0,2})$/.test(octet))) {
    return false;
  }
  const numericOctets = octets.map(Number);
  return numericOctets.every((octet) => octet >= 0 && octet <= 255)
    && numericOctets[0] === 127;
}

function normalizedHostname(url: URL): string {
  return url.hostname.startsWith("[") && url.hostname.endsWith("]")
    ? url.hostname.slice(1, -1)
    : url.hostname;
}

function validLiteralPort(rawPort: string | undefined): boolean {
  if (rawPort === undefined) return true;
  if (!/^(?:[1-9]|[1-9][0-9]{1,4})$/.test(rawPort)) return false;
  return Number(rawPort) <= 65_535;
}

function rawLiteralLoopbackAuthority(rawUrl: string, parsed: URL): boolean {
  if (rawUrl !== rawUrl.trim() || rawUrl.includes("\\")) return false;
  const match = /^[a-z][a-z0-9+.-]*:\/\/([^/?#]+)(?:[/?#]|$)/i.exec(rawUrl);
  if (!match || match[1].includes("@")) return false;

  const authority = match[1];
  const hostname = normalizedHostname(parsed);
  if (authority.startsWith("[")) {
    const ipv6 = /^\[([^\]]+)\](?::([0-9]+))?$/.exec(authority);
    return Boolean(
      ipv6
      && isIP(ipv6[1]) === 6
      && hostname === "::1"
      && validLiteralPort(ipv6[2]),
    );
  }

  const ipv4 = /^([^:]+)(?::([0-9]+))?$/.exec(authority);
  return Boolean(
    ipv4
    && isIP(ipv4[1]) === 4
    && isLiteralIpv4Loopback(ipv4[1])
    && hostname === ipv4[1]
    && validLiteralPort(ipv4[2]),
  );
}

export function isLiteralLoopbackUrl(
  rawUrl: string | URL,
  protocols: ReadonlySet<string> = new Set(["http:", "https:"]),
): boolean {
  try {
    const source = rawUrl instanceof URL ? rawUrl.toString() : rawUrl;
    const parsed = rawUrl instanceof URL ? rawUrl : new URL(rawUrl);
    const hostname = normalizedHostname(parsed);
    return protocols.has(parsed.protocol)
      && parsed.username === ""
      && parsed.password === ""
      && (hostname === "::1" || isLiteralIpv4Loopback(hostname))
      && rawLiteralLoopbackAuthority(source, parsed);
  } catch {
    return false;
  }
}

export type ElectronNetworkTarget =
  | { kind: "url"; value: string | URL }
  | { kind: "unix"; socketPath: string };

/**
 * Applies the process profile to an explicit network target. Offline permits
 * only literal IP loopback URLs and absolute Unix-domain socket paths.
 */
export function requireElectronNetworkTarget(
  target: ElectronNetworkTarget,
  profile: MemoLensNetworkProfile = parseMemoLensNetworkProfile(),
): void {
  if (profile === "online") {
    increment("allowedOnline");
    return;
  }

  if (target.kind === "unix") {
    if (isAbsolute(target.socketPath) && !target.socketPath.includes("\0")) {
      increment("allowedUnixSocket");
      return;
    }
    increment("deniedMalformed");
    throw new ElectronNetworkPolicyError(
      "malformed_target",
      "Offline network policy rejected a malformed local target.",
    );
  }

  if (isLiteralLoopbackUrl(target.value)) {
    increment("allowedLoopback");
    return;
  }
  try {
    void new URL(target.value);
  } catch {
    increment("deniedMalformed");
    throw new ElectronNetworkPolicyError(
      "malformed_target",
      "Offline network policy rejected a malformed local target.",
    );
  }
  increment("deniedRemote");
  throw new ElectronNetworkPolicyError(
    "network_denied",
    "Offline network policy denied a non-loopback request.",
  );
}

export type ElectronExternalNavigationDispatcher = (
  targetUrl: string,
) => Promise<unknown> | unknown;

/**
 * OS-owned URL dispatch is intentionally outside the Chromium Session
 * boundary. It is therefore an online-only action and must pass this explicit
 * profile preflight before shell.openExternal (or any equivalent dispatcher)
 * is reached.
 */
export async function dispatchExternalNavigation(
  targetUrl: string,
  dispatch: ElectronExternalNavigationDispatcher,
  profile: MemoLensNetworkProfile = parseMemoLensNetworkProfile(),
): Promise<void> {
  if (profile !== "online") {
    increment("deniedRemote");
    throw new ElectronNetworkPolicyError(
      "network_denied",
      "Offline network policy denied operating-system external navigation.",
    );
  }
  increment("allowedOnline");
  await dispatch(targetUrl);
}

function fetchTarget(input: Parameters<typeof fetch>[0]): string | URL {
  if (typeof input === "string" || input instanceof URL) {
    return input;
  }
  return input.url;
}

export function createGuardedFetch(fetchImpl: typeof fetch): typeof fetch {
  return async (input, init): Promise<Response> => {
    const profile = parseMemoLensNetworkProfile();
    requireElectronNetworkTarget({ kind: "url", value: fetchTarget(input) }, profile);
    const guardedInit = profile === "offline"
      ? { ...init, redirect: "error" as const }
      : init;
    return fetchImpl(input, guardedInit);
  };
}

/** Uses the current global implementation so test and runtime instrumentation remains observable. */
export const guardedFetch: typeof fetch = async (
  input,
  init,
): Promise<Response> => createGuardedFetch(globalThis.fetch)(input, init);

const CHROMIUM_NETWORK_PROTOCOLS = new Set(["http:", "https:", "ws:", "wss:"]);

function allowChromiumRequest(rawUrl: string): boolean {
  try {
    const parsed = new URL(rawUrl);
    if (!CHROMIUM_NETWORK_PROTOCOLS.has(parsed.protocol)) {
      increment("deniedMalformed");
      return false;
    }
    if (isLiteralLoopbackUrl(parsed, CHROMIUM_NETWORK_PROTOCOLS)) {
      increment("allowedLoopback");
      return true;
    }
  } catch {
    increment("deniedMalformed");
    return false;
  }
  increment("deniedRemote");
  return false;
}

/**
 * Installs the Electron-owned half of offline enforcement. This covers
 * app-declared fetch calls (through guardedFetch) and Chromium requests made
 * by this Session. It is not an OS packet filter and makes no claim about
 * traffic emitted by unrelated processes or native libraries.
 */
export async function configureElectronSessionNetworkPolicy(
  targetSession: Electron.Session,
  profile: MemoLensNetworkProfile = parseMemoLensNetworkProfile(),
): Promise<MemoLensNetworkProfile> {
  if (profile === "online") {
    return profile;
  }

  await targetSession.setProxy({ mode: "direct" });
  targetSession.webRequest.onBeforeRequest(
    { urls: ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"] },
    (details, callback) => {
      callback({ cancel: !allowChromiumRequest(details.url) });
    },
  );
  return profile;
}
