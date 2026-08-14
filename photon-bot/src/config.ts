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
  discordAllowedUserIds: string[];
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

  // This is deliberately parsed before filesystem and network settings so a
  // missing privacy boundary is the first startup error an operator sees.
  const discordAllowedUserIds = readRequiredStringList(
    process.env.DISCORD_ALLOWED_USER_IDS,
    "DISCORD_ALLOWED_USER_IDS",
  );

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
  const explicitDbPath = process.env.SQLITE_DB_PATH?.trim();
  const dbPath = backendSendPathOverrides
    ? readFilePath(
        explicitDbPath || resolveManagedSqlitePath() || "",
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
    discordAllowedUserIds,
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

function memolensStateDir(): string {
  const configured = process.env.MEMOLENS_APP_STATE_DIR?.trim();
  if (configured) {
    return path.resolve(configured);
  }
  if (process.platform === "darwin") {
    return path.join(process.env.HOME ?? "", "Library/Application Support/MemoLens");
  }
  if (process.platform === "win32") {
    return path.join(process.env.APPDATA || path.join(process.env.HOME ?? "", "AppData/Roaming"), "MemoLens");
  }
  return path.join(process.env.XDG_STATE_HOME || path.join(process.env.HOME ?? "", ".local/state"), "MemoLens");
}

function readJsonObject(filePath: string): Record<string, unknown> | null {
  try {
    const payload = JSON.parse(fs.readFileSync(filePath, "utf8")) as unknown;
    return payload !== null && typeof payload === "object" && !Array.isArray(payload)
      ? (payload as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function existingFile(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) {
    return null;
  }
  const resolved = path.resolve(value.trim());
  return fs.existsSync(resolved) && fs.statSync(resolved).isFile() ? resolved : null;
}

function resolveManagedSqlitePath(): string | null {
  const stateDir = memolensStateDir();
  const desktop = readJsonObject(path.join(stateDir, "desktop-settings.json"));
  const fromDesktop = existingFile(desktop?.defaultDbPath);
  if (fromDesktop) {
    return fromDesktop;
  }
  const backend = readJsonObject(path.join(stateDir, "backend-settings.json"));
  const fromBackend = existingFile(backend?.db_path);
  if (fromBackend) {
    return fromBackend;
  }
  const storageDir = path.join(stateDir, "storage");
  if (fs.existsSync(storageDir) && fs.statSync(storageDir).isDirectory()) {
    const hashed = fs
      .readdirSync(storageDir)
      .filter((name) => /^photo-index-[0-9a-f]{24}\.db$/i.test(name))
      .map((name) => path.join(storageDir, name))
      .filter((filePath) => fs.statSync(filePath).isFile())
      .sort();
    if (hashed.length === 1) {
      const onlyHashedPath = hashed[0];
      if (onlyHashedPath) {
        return onlyHashedPath;
      }
    }
  }
  return existingFile(path.join(storageDir, "photo_index.db"));
}

function readFilePath(value: string, key: string): string {
  if (!value.trim()) {
    throw new Error(
      `${key} must be set to the SQLite path shown in MemoLens Library / Setup. Desktop indexes use hashed names like photo-index-<hash>.db under Application Support, not photo_index.db inside the photo folder.`,
    );
  }
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

  return [
    ...new Set(
      rawValue
        .split(",")
        .map((item) => item.trim())
        .filter((item) => item.length > 0),
    ),
  ];
}

function readRequiredStringList(rawValue: string | undefined, key: string): string[] {
  const values = readStringList(rawValue);
  if (values.length === 0) {
    throw new Error(
      `${key} must contain at least one Discord user ID. Refusing to start with an empty user allowlist.`,
    );
  }
  return values;
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
