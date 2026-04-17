import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export type LogLevel = "debug" | "info" | "warn" | "error";

export type BotConfig = {
  backendBaseUrl: string;
  imageLibraryDir: string;
  dbPath: string | null;
  backendSendPathOverrides: boolean;
  discordSendImageWidth: number;
  discordBotToken: string;
  discordAllowedChannelIds: string[];
  backendRequestTimeoutMs: number;
  defaultTopK: number;
  defaultReplyImageCount: number;
  sessionTtlMinutes: number;
  logLevel: LogLevel;
  requestTimeoutMs: number;
};

const currentFile = fileURLToPath(import.meta.url);
const currentDir = path.dirname(currentFile);
const botRoot = path.resolve(currentDir, "..");
const projectRoot = path.resolve(botRoot, "..");
const defaultImageLibraryDir = path.resolve(projectRoot, "local-photo-library");

let envLoaded = false;

export function loadConfig(): BotConfig {
  loadEnvFiles();

  const backendBaseUrl = readUrl(
    process.env.BACKEND_BASE_URL ?? "http://127.0.0.1:5519",
    "BACKEND_BASE_URL",
  );
  const imageLibraryDir = readDirectory(
    process.env.IMAGE_LIBRARY_DIR ?? defaultImageLibraryDir,
    "IMAGE_LIBRARY_DIR",
    process.env.IMAGE_LIBRARY_DIR === undefined,
  );
  const backendSendPathOverrides = readBoolean(
    process.env.BACKEND_SEND_PATH_OVERRIDES,
    true,
  );
  const discordSendImageWidth = readInteger(
    process.env.DISCORD_SEND_IMAGE_WIDTH,
    512,
    "DISCORD_SEND_IMAGE_WIDTH",
    64,
  );
  const dbPath = backendSendPathOverrides
    ? readFilePath(
        process.env.SQLITE_DB_PATH ?? path.join(imageLibraryDir, "photo_index.db"),
        "SQLITE_DB_PATH",
      )
    : readOptionalFilePath(process.env.SQLITE_DB_PATH);
  const discordBotToken = readRequiredString(process.env.DISCORD_BOT_TOKEN, "DISCORD_BOT_TOKEN");
  const discordAllowedChannelIds = readStringList(process.env.DISCORD_ALLOWED_CHANNEL_IDS);
  const backendRequestTimeoutMs = readInteger(
    process.env.BACKEND_REQUEST_TIMEOUT_MS,
    180_000,
    "BACKEND_REQUEST_TIMEOUT_MS",
    1_000,
  );
  const defaultReplyImageCount = readInteger(
    process.env.DEFAULT_REPLY_IMAGE_COUNT,
    9,
    "DEFAULT_REPLY_IMAGE_COUNT",
    1,
    9,
  );
  const defaultTopK = readInteger(
    process.env.DEFAULT_TOP_K,
    9,
    "DEFAULT_TOP_K",
    defaultReplyImageCount,
  );
  const sessionTtlMinutes = readInteger(
    process.env.SESSION_TTL_MINUTES,
    30,
    "SESSION_TTL_MINUTES",
    1,
  );
  const rawLogLevel = (process.env.LOG_LEVEL ?? "info").trim().toLowerCase();
  const logLevel = isLogLevel(rawLogLevel) ? rawLogLevel : "info";

  return {
    backendBaseUrl,
    imageLibraryDir,
    dbPath,
    backendSendPathOverrides,
    discordSendImageWidth,
    discordBotToken,
    discordAllowedChannelIds,
    backendRequestTimeoutMs,
    defaultTopK,
    defaultReplyImageCount,
    sessionTtlMinutes,
    logLevel,
    requestTimeoutMs: backendRequestTimeoutMs,
  };
}

function loadEnvFiles(): void {
  if (envLoaded) {
    return;
  }

  for (const envPath of [path.join(projectRoot, ".env"), path.join(botRoot, ".env")]) {
    if (!fs.existsSync(envPath)) {
      continue;
    }

    const content = fs.readFileSync(envPath, "utf8");
    for (const rawLine of content.split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line || line.startsWith("#") || !line.includes("=")) {
        continue;
      }

      const [rawKey = "", ...rawValueParts] = line.split("=");
      const key = rawKey.trim();
      if (!key || process.env[key] !== undefined) {
        continue;
      }

      const rawValue = rawValueParts.join("=").trim();
      process.env[key] = stripWrappingQuotes(rawValue);
    }
  }

  envLoaded = true;
}

function stripWrappingQuotes(value: string): string {
  if (value.length >= 2 && value[0] === value[value.length - 1] && `"'`.includes(value[0]!)) {
    return value.slice(1, -1);
  }
  return value;
}

function readUrl(value: string, key: string): string {
  try {
    return new URL(value).toString().replace(/\/$/, "");
  } catch {
    throw new Error(`${key} must be a valid absolute URL.`);
  }
}

function readDirectory(value: string, key: string, createIfMissing = false): string {
  const resolved = path.resolve(value);
  if (!fs.existsSync(resolved)) {
    if (!createIfMissing) {
      throw new Error(`${key} does not exist: ${resolved}`);
    }
    fs.mkdirSync(resolved, { recursive: true });
  }
  if (!fs.statSync(resolved).isDirectory()) {
    throw new Error(`${key} must point to a directory: ${resolved}`);
  }
  return resolved;
}

function readFilePath(value: string, key: string): string {
  const resolved = path.resolve(value);
  if (!fs.existsSync(resolved)) {
    throw new Error(`${key} does not exist: ${resolved}`);
  }
  if (!fs.statSync(resolved).isFile()) {
    throw new Error(`${key} must point to a file: ${resolved}`);
  }
  return resolved;
}

function readOptionalFilePath(value: string | undefined): string | null {
  if (value === undefined || !value.trim()) {
    return null;
  }

  const resolved = path.resolve(value);
  if (!fs.existsSync(resolved) || !fs.statSync(resolved).isFile()) {
    return null;
  }
  return resolved;
}

function readRequiredString(rawValue: string | undefined, key: string): string {
  const value = rawValue?.trim();
  if (!value) {
    throw new Error(`${key} must be set.`);
  }
  return value;
}

function readStringList(rawValue: string | undefined): string[] {
  if (!rawValue) {
    return [];
  }

  return rawValue
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

function readInteger(
  rawValue: string | undefined,
  fallback: number,
  key: string,
  min: number,
  max?: number,
): number {
  const value = rawValue === undefined ? fallback : Number.parseInt(rawValue, 10);
  if (!Number.isFinite(value) || !Number.isInteger(value)) {
    throw new Error(`${key} must be an integer.`);
  }
  if (value < min) {
    throw new Error(`${key} must be >= ${min}.`);
  }
  if (max !== undefined && value > max) {
    throw new Error(`${key} must be <= ${max}.`);
  }
  return value;
}

function readBoolean(rawValue: string | undefined, fallback: boolean): boolean {
  if (rawValue === undefined) {
    return fallback;
  }
  return ["1", "true", "yes", "on"].includes(rawValue.trim().toLowerCase());
}

function isLogLevel(value: string): value is LogLevel {
  return value === "debug" || value === "info" || value === "warn" || value === "error";
}
