import {
  spawn,
  type ChildProcess,
} from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import type {
  DesktopBackendStatus,
  DesktopSettings,
} from "../src/query/types.js";

const DEFAULT_STARTUP_TIMEOUT_MS = 30000;
const DEFAULT_HEALTH_POLL_INTERVAL_MS = 500;
const DEFAULT_RETRY_DELAY_MS = 1000;

interface BackendSpawnOptions {
  cwd: string;
  env: NodeJS.ProcessEnv;
  stdio: ["ignore", "pipe", "pipe"];
}

type SpawnBackendProcess = (
  command: string,
  args: string[],
  options: BackendSpawnOptions,
) => ChildProcess;

interface BackendLogger {
  log(message: string): void;
  error(message: string): void;
}

export interface BackendProcessSupervisorOptions {
  backendUrl: string;
  probeHealth(url: string): Promise<boolean>;
  getSessionToken(): string;
  rotateSessionToken(): void;
  updateBackendTrust(trusted: boolean): void;
  getAppStateDir(): string;
  spawnProcess?: SpawnBackendProcess;
  readEnvFile?: (path: string) => string;
  environment?: () => NodeJS.ProcessEnv;
  now?: () => number;
  sleep?: (durationMs: number) => Promise<void>;
  logger?: BackendLogger;
  startupTimeoutMs?: number;
  healthPollIntervalMs?: number;
  retryDelayMs?: number;
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

function defaultSleep(durationMs: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, durationMs);
  });
}

export class BackendProcessSupervisor {
  private readonly backendUrl: string;
  private readonly probeHealth: (url: string) => Promise<boolean>;
  private readonly getSessionToken: () => string;
  private readonly rotateSessionToken: () => void;
  private readonly updateBackendTrust: (trusted: boolean) => void;
  private readonly getAppStateDir: () => string;
  private readonly spawnProcess: SpawnBackendProcess;
  private readonly readEnvFile: (path: string) => string;
  private readonly environment: () => NodeJS.ProcessEnv;
  private readonly now: () => number;
  private readonly sleep: (durationMs: number) => Promise<void>;
  private readonly logger: BackendLogger;
  private readonly startupTimeoutMs: number;
  private readonly healthPollIntervalMs: number;
  private readonly retryDelayMs: number;

  private managedProcess: ChildProcess | null = null;
  private startError: string | null = null;

  constructor(options: BackendProcessSupervisorOptions) {
    this.backendUrl = normalizeBackendUrl(options.backendUrl);
    this.probeHealth = options.probeHealth;
    this.getSessionToken = options.getSessionToken;
    this.rotateSessionToken = options.rotateSessionToken;
    this.updateBackendTrust = options.updateBackendTrust;
    this.getAppStateDir = options.getAppStateDir;
    this.spawnProcess = options.spawnProcess
      ?? ((command, args, spawnOptions) => spawn(command, args, spawnOptions));
    this.readEnvFile = options.readEnvFile ?? ((path) => readFileSync(path, "utf-8"));
    this.environment = options.environment ?? (() => process.env);
    this.now = options.now ?? Date.now;
    this.sleep = options.sleep ?? defaultSleep;
    this.logger = options.logger ?? console;
    this.startupTimeoutMs = options.startupTimeoutMs ?? DEFAULT_STARTUP_TIMEOUT_MS;
    this.healthPollIntervalMs = options.healthPollIntervalMs
      ?? DEFAULT_HEALTH_POLL_INTERVAL_MS;
    this.retryDelayMs = options.retryDelayMs ?? DEFAULT_RETRY_DELAY_MS;
  }

  async ensureReady(
    projectRoot: string,
    settings: DesktopSettings,
  ): Promise<DesktopBackendStatus> {
    // A health proof is required on every ensure operation. Until it succeeds,
    // the Electron session must not attach the bearer to renderer traffic.
    this.updateBackendTrust(false);

    if (await this.probeHealth(this.backendUrl)) {
      this.updateBackendTrust(true);
      return {
        state: "connected",
        message: "Local backend is online.",
        url: this.backendUrl,
        startedByApp: false,
      };
    }

    if (!settings.autoStartBackend) {
      return {
        state: "unavailable",
        message: "Backend is offline. Enable auto-start or launch the Python service manually.",
        url: this.backendUrl,
        startedByApp: false,
      };
    }

    // Kill any previously managed process that has exited or is unresponsive.
    if (this.managedProcess !== null && this.managedProcess.exitCode !== null) {
      this.killManagedProcess();
    }

    if (this.managedProcess === null) {
      this.spawnAttempt(projectRoot, settings, this.backendUrl);
    }

    if (await this.waitForHealthy(this.backendUrl)) {
      return this.startedStatus("Local backend started by the desktop app.");
    }

    // First attempt failed — kill the process and try once more.
    this.logger.log("[memolens-backend] first start attempt failed, retrying…");
    this.killManagedProcess();
    await this.sleep(this.retryDelayMs);
    this.spawnAttempt(projectRoot, settings, this.backendUrl);

    if (await this.waitForHealthy(this.backendUrl)) {
      return this.startedStatus("Local backend started by the desktop app (retry succeeded).");
    }

    return {
      state: "unavailable",
      message: this.startError
        ? `Backend failed to start: ${this.startError}`
        : `Backend did not become healthy. Check the Python environment configured in the desktop settings (${settings.pythonCommand}).`,
      url: this.backendUrl,
      startedByApp: true,
    };
  }

  stop(): void {
    this.rotateSessionToken();
    if (this.managedProcess !== null && this.managedProcess.exitCode === null) {
      this.managedProcess.kill("SIGTERM");
    }
  }

  private startedStatus(message: string): DesktopBackendStatus {
    this.updateBackendTrust(true);
    return {
      state: "started",
      message,
      url: this.backendUrl,
      startedByApp: true,
    };
  }

  private spawnAttempt(
    projectRoot: string,
    settings: DesktopSettings,
    url: string,
  ): void {
    this.startError = null;
    const nextProcess = this.spawnProcess(settings.pythonCommand, ["backend/app.py"], {
      cwd: projectRoot,
      env: {
        ...this.loadDotEnvVars(projectRoot),
        ...this.environment(),
        MEMOLENS_APP_STATE_DIR: this.getAppStateDir(),
        MEMOLENS_BACKEND_PORT: resolveBackendPort(url),
        MEMOLENS_BACKEND_DEBUG: "0",
        MEMOLENS_DESKTOP_SESSION_TOKEN: this.getSessionToken(),
        PYTHONUNBUFFERED: "1",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    this.managedProcess = nextProcess;
    this.attachLogging(nextProcess);
  }

  private attachLogging(processRef: ChildProcess): void {
    processRef.on("error", (error: Error) => {
      this.startError = error.message;
      this.logger.error(`[memolens-backend] failed to start: ${error.message}`);
      if (this.managedProcess === processRef) {
        this.managedProcess = null;
        this.rotateSessionToken();
      }
    });

    processRef.stdout?.on("data", (chunk: Buffer) => {
      const message = chunk.toString().trim();
      if (message.length > 0) {
        this.logger.log(`[memolens-backend] ${message}`);
      }
    });

    processRef.stderr?.on("data", (chunk: Buffer) => {
      const message = chunk.toString().trim();
      if (message.length > 0) {
        this.logger.error(`[memolens-backend] ${message}`);
      }
    });

    processRef.on("exit", (code, signal) => {
      this.logger.log(
        `[memolens-backend] exited with code=${code ?? "null"} signal=${signal ?? "null"}`,
      );
      if (this.managedProcess === processRef) {
        this.managedProcess = null;
        this.rotateSessionToken();
      }
    });
  }

  private async waitForHealthy(url: string): Promise<boolean> {
    const deadline = this.now() + this.startupTimeoutMs;
    while (this.now() < deadline) {
      if (await this.probeHealth(url)) {
        this.startError = null;
        return true;
      }

      if (this.startError) {
        return false;
      }

      if (this.managedProcess === null || this.managedProcess.exitCode !== null) {
        return false;
      }

      await this.sleep(this.healthPollIntervalMs);
    }
    return false;
  }

  private loadDotEnvVars(projectRoot: string): Record<string, string> {
    const envVars: Record<string, string> = {};
    const envFiles = [
      join(projectRoot, ".env"),
      join(projectRoot, "backend", ".env"),
    ];

    for (const envPath of envFiles) {
      try {
        const content = this.readEnvFile(envPath);
        for (const rawLine of content.split("\n")) {
          const line = rawLine.trim();
          if (!line || line.startsWith("#") || !line.includes("=")) {
            continue;
          }
          const eqIndex = line.indexOf("=");
          const key = line.slice(0, eqIndex).trim();
          let value = line.slice(eqIndex + 1).trim();
          if (
            value.length >= 2
            && value[0] === value[value.length - 1]
            && (value[0] === "'" || value[0] === "\"" || value[0] === "`")
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

  private killManagedProcess(): void {
    this.rotateSessionToken();
    if (this.managedProcess !== null) {
      try {
        if (this.managedProcess.exitCode === null) {
          this.managedProcess.kill("SIGTERM");
        }
      } catch {
        // ignore
      }
      this.managedProcess = null;
    }
  }
}
