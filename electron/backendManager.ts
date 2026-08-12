import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

import type {
  DesktopBackendStatus,
  DesktopSettings,
} from "../src/query/types.js";
import { BackendProcessSupervisor } from "./backendProcessSupervisor.js";
import { getCanonicalAppStateDir } from "./appPaths.js";
import { DEFAULT_BACKEND_URL } from "./desktopSettings.js";

const EXPECTED_BACKEND_SERVICE = "memolens-backend";
const EXPECTED_API_VERSION = "1";
let desktopSessionToken = randomBytes(32).toString("hex");

let backendIdentityVerified = false;

function updateBackendTrust(trusted: boolean): void {
  backendIdentityVerified = trusted;
}

function rotateDesktopSessionToken(): void {
  desktopSessionToken = randomBytes(32).toString("hex");
  updateBackendTrust(false);
}

export function getDesktopSessionToken(): string {
  return desktopSessionToken;
}

export function isBackendIdentityVerified(): boolean {
  return backendIdentityVerified;
}

export function revokeBackendTrust(): void {
  rotateDesktopSessionToken();
}

export function markBackendTrustVerified(): void {
  updateBackendTrust(true);
}

export function verifyBackendHealthPayload(
  payload: unknown,
  challenge: string,
): boolean {
  if (payload === null || typeof payload !== "object") {
    return false;
  }
  const health = payload as Record<string, unknown>;
  const suppliedProof = typeof health.challenge_proof === "string"
    ? health.challenge_proof
    : "";
  if (!/^[0-9a-f]{64}$/.test(suppliedProof)) {
    return false;
  }

  const expectedProof = createHmac("sha256", getDesktopSessionToken())
    .update(challenge)
    .digest();
  const suppliedProofBytes = Buffer.from(suppliedProof, "hex");
  return health.status === "ok"
    && health.service === EXPECTED_BACKEND_SERVICE
    && health.api_version === EXPECTED_API_VERSION
    && timingSafeEqual(suppliedProofBytes, expectedProof);
}

async function isBackendHealthy(url: string): Promise<boolean> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 1500);
  const challenge = randomBytes(32).toString("hex");

  try {
    const response = await fetch(
      `${url}/healthz?challenge=${challenge}`,
      {
        signal: controller.signal,
      },
    );
    if (!response.ok) {
      return false;
    }
    return verifyBackendHealthPayload(await response.json(), challenge);
  } catch {
    return false;
  } finally {
    clearTimeout(timeoutId);
  }
}

const backendProcessSupervisor = new BackendProcessSupervisor({
  backendUrl: DEFAULT_BACKEND_URL,
  probeHealth: isBackendHealthy,
  getSessionToken: getDesktopSessionToken,
  rotateSessionToken: rotateDesktopSessionToken,
  updateBackendTrust,
  getAppStateDir: getCanonicalAppStateDir,
});

export async function ensureBackendReady(
  projectRoot: string,
  settings: DesktopSettings,
): Promise<DesktopBackendStatus> {
  return backendProcessSupervisor.ensureReady(projectRoot, settings);
}

export function stopManagedBackend(): void {
  backendProcessSupervisor.stop();
}
