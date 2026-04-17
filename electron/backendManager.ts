import {
  spawn,
  type ChildProcess,
} from "node:child_process";

import type {
  DesktopBackendStatus,
  DesktopSettings,
} from "../src/query/types.js";
import { getCanonicalAppStateDir } from "./appPaths.js";

const BACKEND_STARTUP_TIMEOUT_MS = 15000;
const HEALTH_POLL_INTERVAL_MS = 500;

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

function canAutoStartBackend(url: string): boolean {
  try {
    const parsed = new URL(url);
    return (
      parsed.protocol === "http:" &&
      (parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost")
    );
  } catch {
    return false;
  }
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

async function isBackendHealthy(url: string): Promise<boolean> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 1500);

  try {
    const response = await fetch(`${normalizeBackendUrl(url)}/healthz`, {
      signal: controller.signal,
    });
    return response.ok;
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

export async function ensureBackendReady(
  projectRoot: string,
  settings: DesktopSettings,
): Promise<DesktopBackendStatus> {
  const url = normalizeBackendUrl(settings.backendUrl);

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

  if (!canAutoStartBackend(url)) {
    return {
      state: "unavailable",
      message: "Desktop auto-start only supports localhost backend URLs.",
      url,
      startedByApp: false,
    };
  }

  if (managedBackendProcess === null || managedBackendProcess.exitCode !== null) {
    managedBackendStartError = null;
    const nextProcess = spawn(settings.pythonCommand, ["backend/app.py"], {
      cwd: projectRoot,
      env: {
        ...process.env,
        MEMOLENS_APP_STATE_DIR: getCanonicalAppStateDir(),
        MEMOLENS_BACKEND_PORT: resolveBackendPort(url),
        MEMOLENS_BACKEND_DEBUG: "0",
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
