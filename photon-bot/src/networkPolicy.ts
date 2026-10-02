import { isIP } from "node:net";

export type MemoLensNetworkProfile = "online" | "offline";

export class PhotonNetworkPolicyError extends Error {
  readonly code = "network_offline_target_denied";
}

const counters = {
  allowedLoopback: 0,
  blockedExternalPlatform: 0,
  blockedNonLoopback: 0,
};

export function networkProfile(
  environment: NodeJS.ProcessEnv = process.env,
): MemoLensNetworkProfile {
  const value = (environment.MEMOLENS_NETWORK_PROFILE ?? "online").trim().toLowerCase();
  if (value !== "online" && value !== "offline") {
    throw new PhotonNetworkPolicyError(
      "MemoLens network profile is invalid; Photon networking is disabled.",
    );
  }
  return value;
}

function literalLoopback(hostname: string): boolean {
  const unwrapped = hostname.startsWith("[") && hostname.endsWith("]")
    ? hostname.slice(1, -1)
    : hostname;
  if (isIP(unwrapped) === 4) {
    return unwrapped.split(".")[0] === "127";
  }
  return isIP(unwrapped) === 6 && unwrapped.toLowerCase() === "::1";
}

function parseLiteralLoopbackUrl(rawUrl: string): URL | null {
  if (rawUrl !== rawUrl.trim()) return null;

  let target: URL;
  try {
    target = new URL(rawUrl);
  } catch {
    return null;
  }
  if (
    (target.protocol !== "http:" && target.protocol !== "https:")
    || target.username !== ""
    || target.password !== ""
  ) {
    return null;
  }

  const authorityMatch = rawUrl.match(/^https?:\/\/([^/?#]+)/i);
  if (!authorityMatch) return null;
  const authority = authorityMatch[1]!;
  if (authority.includes("@")) return null;

  if (authority.startsWith("[")) {
    const closingBracket = authority.indexOf("]");
    if (closingBracket < 0) return null;
    const rawHostname = authority.slice(1, closingBracket);
    const portSuffix = authority.slice(closingBracket + 1);
    if (portSuffix !== "" && !/^:\d+$/.test(portSuffix)) return null;
    return literalLoopback(rawHostname) ? target : null;
  }

  const [rawHostname = "", ...portParts] = authority.split(":");
  if (portParts.length > 1) return null;
  if (portParts.length === 1 && !/^\d+$/.test(portParts[0]!)) return null;
  return literalLoopback(rawHostname) ? target : null;
}

/**
 * Enforce the permanent Photon-to-MemoLens trust boundary. The backend is a
 * local companion service in every network profile, including `online`.
 */
export function requireLocalServiceUrl(
  rawUrl: string,
  environment: NodeJS.ProcessEnv = process.env,
): void {
  networkProfile(environment);
  if (parseLiteralLoopbackUrl(rawUrl)) {
    counters.allowedLoopback += 1;
    return;
  }
  counters.blockedNonLoopback += 1;
  throw new PhotonNetworkPolicyError(
    "MemoLens denied a non-local backend target before transmission.",
  );
}

export function requireLoopbackUrlAllowed(
  rawUrl: string,
  environment: NodeJS.ProcessEnv = process.env,
): void {
  if (networkProfile(environment) !== "offline") return;
  requireLocalServiceUrl(rawUrl, environment);
}

export function requireExternalPlatformAllowed(
  _platform: "discord" | "imessage",
  environment: NodeJS.ProcessEnv = process.env,
): void {
  if (networkProfile(environment) !== "offline") return;
  counters.blockedExternalPlatform += 1;
  throw new PhotonNetworkPolicyError(
    "Offline MemoLens denied an external messaging platform before transmission.",
  );
}

export function resetNetworkPolicyObservation(): void {
  counters.allowedLoopback = 0;
  counters.blockedExternalPlatform = 0;
  counters.blockedNonLoopback = 0;
}

export function networkPolicyObservation(): Record<string, unknown> {
  return {
    object: "memolens.network_policy_observation",
    schema_version: "1",
    profile: networkProfile(),
    scope: {
      process: "photon-node",
      instrumentation: [
        "local-service-preflight",
        "offline-fetch-preflight",
        "external-platform-preflight",
      ],
      os_packet_capture: false,
      dependency_internal_sockets_observed: false,
    },
    counters: { ...counters },
  };
}
