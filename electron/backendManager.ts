import {
  spawn,
  type ChildProcess,
} from "node:child_process";
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import type {
  DesktopBackendStatus,
  DesktopSettings,
} from "../src/query/types.js";
import { getCanonicalAppStateDir } from "./appPaths.js";
import { DEFAULT_BACKEND_URL } from "./desktopSettings.js";

const BACKEND_STARTUP_TIMEOUT_MS = 30000;
const HEALTH_POLL_INTERVAL_MS = 500;
const EXPECTED_BACKEND_SERVICE = "memolens-backend";
const EXPECTED_API_VERSION = "1";
export const DESKTOP_SESSION_TOKEN = randomBytes(32).toString("hex");

let managedBackendProcess: ChildProcess | null = null;
let managedBackendStartError: string | null = null;

function sleep(durationMs: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, durationMs);
  });
}

function normalizeBackendUrl(url: string): string {
  return url.trim().replace(/\/+$/, "");
}

function resolveBackendPort(url: string): string {
  try {
    const parsed = new URL(url);
    if (parsed.port.trim().length > 0) {
      return parsed.port;
    }
    return parsed.protocol === "https:" ? "443" : "80";
  } catch {
    return "5519";
  }
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

  const expectedProof = createHmac("sha256", DESKTOP_SESSION_TOKEN)
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
      `${normalizeBackendUrl(url)}/healthz?challenge=${challenge}`,
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

function attachLogging(processRef: ChildProcess): void {
  processRef.on("error", (error: Error) => {
    managedBackendStartError = error.message;
    console.error(`[memolens-backend] failed to start: ${error.message}`);
    if (managedBackendProcess === processRef) {
      managedBackendProcess = null;
    }
  });

  processRef.stdout?.on("data", (chunk: Buffer) => {
    const message = chunk.toString().trim();
    if (message.length > 0) {
      console.log(`[memolens-backend] ${message}`);
    }
  });

  processRef.stderr?.on("data", (chunk: Buffer) => {
    const message = chunk.toString().trim();
    if (message.length > 0) {
      console.error(`[memolens-backend] ${message}`);
    }
  });

  processRef.on("exit", (code, signal) => {
    console.log(
      `[memolens-backend] exited with code=${code ?? "null"} signal=${signal ?? "null"}`,
    );
    if (managedBackendProcess === processRef) {
      managedBackendProcess = null;
    }
  });
}

async function waitForHealthy(url: string): Promise<boolean> {
  const deadline = Date.now() + BACKEND_STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await isBackendHealthy(url)) {
      managedBackendStartError = null;
      return true;
    }

    if (managedBackendStartError) {
      return false;
    }

    if (managedBackendProcess === null || managedBackendProcess.exitCode !== null) {
      return false;
    }

    await sleep(HEALTH_POLL_INTERVAL_MS);
  }
  return false;
}

function loadDotEnvVars(projectRoot: string): Record<string, string> {
  const envVars: Record<string, string> = {};
  const envFiles = [
    join(projectRoot, ".env"),
    join(projectRoot, "backend", ".env"),
  ];

  for (const envPath of envFiles) {
    try {
      const content = readFileSync(envPath, "utf-8");
      for (const rawLine of content.split("\n")) {
        const line = rawLine.trim();
        if (!line || line.startsWith("#") || !line.includes("=")) {
          continue;
        }
        const eqIndex = line.indexOf("=");
        const key = line.slice(0, eqIndex).trim();
        let value = line.slice(eqIndex + 1).trim();
        if (
          value.length >= 2 &&
          value[0] === value[value.length - 1] &&
          (value[0] === "'" || value[0] === '"' || value[0] === "`")
        ) {
          value = value.slice(1, -1);
        }
        if (key) {
          envVars[key] = value;
        }
      }
    } catch {
      // .env file may not exist; that is fine.
    }
  }

  return envVars;
}

function killManagedProcess(): void {
  if (managedBackendProcess !== null) {
    try {
      if (managedBackendProcess.exitCode === null) {
        managedBackendProcess.kill("SIGTERM");
      }
    } catch {
      // ignore
    }
    managedBackendProcess = null;
  }
}

export async function ensureBackendReady(
  projectRoot: string,
  settings: DesktopSettings,
): Promise<DesktopBackendStatus> {
  const url = normalizeBackendUrl(DEFAULT_BACKEND_URL);

  if (await isBackendHealthy(url)) {
    return {
      state: "connected",
      message: "Local backend is online.",
      url,
      startedByApp: false,
    };
  }

  if (!settings.autoStartBackend) {
    return {
      state: "unavailable",
      message: "Backend is offline. Enable auto-start or launch the Python service manually.",
      url,
      startedByApp: false,
    };
  }

  // Kill any previously managed process that has exited or is unresponsive.
  if (managedBackendProcess !== null && managedBackendProcess.exitCode !== null) {
    killManagedProcess();
  }

  if (managedBackendProcess === null) {
    managedBackendStartError = null;
    const dotEnvVars = loadDotEnvVars(projectRoot);
    const nextProcess = spawn(settings.pythonCommand, ["backend/app.py"], {
      cwd: projectRoot,
      env: {
        ...dotEnvVars,
        ...process.env,
        MEMOLENS_APP_STATE_DIR: getCanonicalAppStateDir(),
        MEMOLENS_BACKEND_PORT: resolveBackendPort(url),
        MEMOLENS_BACKEND_DEBUG: "0",
        MEMOLENS_DESKTOP_SESSION_TOKEN: DESKTOP_SESSION_TOKEN,
        PYTHONUNBUFFERED: "1",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    managedBackendProcess = nextProcess;
    attachLogging(nextProcess);
  }

  if (await waitForHealthy(url)) {
    return {
      state: "started",
      message: "Local backend started by the desktop app.",
      url,
      startedByApp: true,
    };
  }

  // First attempt failed — kill the process and try once more.
  console.log("[memolens-backend] first start attempt failed, retrying…");
  killManagedProcess();
  await sleep(1000);

  managedBackendStartError = null;
  const dotEnvVars = loadDotEnvVars(projectRoot);
  const retryProcess = spawn(settings.pythonCommand, ["backend/app.py"], {
    cwd: projectRoot,
    env: {
      ...dotEnvVars,
      ...process.env,
      MEMOLENS_APP_STATE_DIR: getCanonicalAppStateDir(),
      MEMOLENS_BACKEND_PORT: resolveBackendPort(url),
      MEMOLENS_BACKEND_DEBUG: "0",
      MEMOLENS_DESKTOP_SESSION_TOKEN: DESKTOP_SESSION_TOKEN,
      PYTHONUNBUFFERED: "1",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  managedBackendProcess = retryProcess;
  attachLogging(retryProcess);

  if (await waitForHealthy(url)) {
    return {
      state: "started",
      message: "Local backend started by the desktop app (retry succeeded).",
      url,
      startedByApp: true,
    };
  }

  return {
    state: "unavailable",
    message: managedBackendStartError
      ? `Backend failed to start: ${managedBackendStartError}`
      : `Backend did not become healthy. Check the Python environment configured in the desktop settings (${settings.pythonCommand}).`,
    url,
    startedByApp: true,
  };
}

export function stopManagedBackend(): void {
  if (managedBackendProcess !== null && managedBackendProcess.exitCode === null) {
    managedBackendProcess.kill("SIGTERM");
  }
}
