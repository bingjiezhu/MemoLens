import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { mkdir, open, readFile, rename, unlink } from "node:fs/promises";
import { dirname, join, normalize, resolve } from "node:path";

import type {
  DesktopSettings,
  DesktopSettingsUpdate,
} from "../src/query/types.js";
import { getCanonicalAppStateDir } from "./appPaths.js";
import type { ApprovedDesktopLibraryBinding } from "./librarySelectionAuthority.js";

export const DEFAULT_BACKEND_URL = "http://127.0.0.1:5519";
let settingsMutationQueue: Promise<void> = Promise.resolve();

interface PersistedDesktopSettings {
  schemaVersion: 2;
  autoStartBackend: boolean;
  librarySelectionAuthority?: {
    schemaVersion: 1;
    canonicalRoot: string;
    dbPath: string;
    device: string;
    inode: string;
  };
}

function normalizeLibraryIdentity(folderPath: string): string {
  const normalized = normalize(resolve(folderPath)).normalize("NFC");
  return process.platform === "win32" ? normalized.toLowerCase() : normalized;
}

export function resolveLibraryDbPath(folderPath: string): string {
  return resolveLibraryDbPathForState(folderPath, getCanonicalAppStateDir());
}

export function resolveLibraryDbPathForState(
  folderPath: string,
  stateDir: string,
): string {
  const digest = createHash("sha256")
    .update(normalizeLibraryIdentity(folderPath), "utf8")
    .digest("hex")
    .slice(0, 24);
  return join(
    resolve(stateDir),
    "storage",
    `photo-index-${digest}.db`,
  );
}

function getSettingsPath(): string {
  return join(getCanonicalAppStateDir(), "desktop-settings.json");
}

export function managedPythonCommand(
  projectRoot: string,
  platform: NodeJS.Platform = process.platform,
): string {
  return platform === "win32"
    ? join(projectRoot, ".venv", "Scripts", "python.exe")
    : join(projectRoot, ".venv", "bin", "python");
}

function normalizePersistedBinding(value: unknown): ApprovedDesktopLibraryBinding | null {
  if (value === null || typeof value !== "object") {
    return null;
  }
  const raw = value as Partial<NonNullable<
    PersistedDesktopSettings["librarySelectionAuthority"]
  >>;
  if (
    raw.schemaVersion !== 1
    || typeof raw.canonicalRoot !== "string"
    || typeof raw.dbPath !== "string"
    || typeof raw.device !== "string"
    || typeof raw.inode !== "string"
    || raw.device.length === 0
    || raw.inode.length === 0
  ) {
    return null;
  }
  const canonicalRoot = normalizeLibraryIdentity(raw.canonicalRoot);
  const dbPath = resolve(raw.dbPath);
  if (canonicalRoot !== raw.canonicalRoot || dbPath !== resolveLibraryDbPath(canonicalRoot)) {
    return null;
  }
  return {
    canonicalRoot,
    dbPath,
    device: raw.device,
    inode: raw.inode,
  };
}

async function readPersistedSettings(): Promise<PersistedDesktopSettings> {
  try {
    const raw = JSON.parse(await readFile(getSettingsPath(), "utf-8")) as unknown;
    const record = raw !== null && typeof raw === "object"
      ? raw as Record<string, unknown>
      : {};
    const binding = normalizePersistedBinding(record.librarySelectionAuthority);
    return {
      schemaVersion: 2,
      autoStartBackend: typeof record.autoStartBackend === "boolean"
        ? record.autoStartBackend
        : true,
      ...(binding === null ? {} : {
        librarySelectionAuthority: {
          schemaVersion: 1,
          ...binding,
        },
      }),
    };
  } catch {
    return { schemaVersion: 2, autoStartBackend: true };
  }
}

async function writePersistedSettings(settings: PersistedDesktopSettings): Promise<void> {
  return writeSettingsProjection(getSettingsPath(), settings);
}

async function writeSettingsProjection(settingsPath: string, settings: unknown): Promise<void> {
  const settingsDirectory = dirname(settingsPath);
  const temporaryPath = `${settingsPath}.${randomUUID()}.tmp`;
  await mkdir(settingsDirectory, { recursive: true });
  const temporaryHandle = await open(
    temporaryPath,
    constants.O_WRONLY
      | constants.O_CREAT
      | constants.O_EXCL
      | (constants.O_NOFOLLOW ?? 0),
    0o600,
  );
  try {
    await temporaryHandle.chmod(0o600);
    await temporaryHandle.writeFile(`${JSON.stringify(settings, null, 2)}\n`, {
      encoding: "utf-8",
    });
    await temporaryHandle.sync();
    await temporaryHandle.close();
  } catch (error) {
    await temporaryHandle.close().catch(() => undefined);
    await unlink(temporaryPath).catch(() => undefined);
    throw error;
  }
  try {
    await rename(temporaryPath, settingsPath);
    const directoryHandle = await open(
      settingsDirectory,
      constants.O_RDONLY
        | (constants.O_DIRECTORY ?? 0)
        | (constants.O_NOFOLLOW ?? 0),
    );
    try {
      await directoryHandle.sync();
    } finally {
      await directoryHandle.close();
    }
  } catch (error) {
    await unlink(temporaryPath).catch(() => undefined);
    throw error;
  }
}

function mutatePersistedSettings<T>(operation: () => Promise<T>): Promise<T> {
  const result = settingsMutationQueue.then(operation, operation);
  settingsMutationQueue = result.then(
    () => undefined,
    () => undefined,
  );
  return result;
}

function toDesktopSettings(
  projectRoot: string,
  persisted: PersistedDesktopSettings,
): DesktopSettings {
  const suggestedLibraryDir = resolve(projectRoot, "local-photo-library");
  const binding = normalizePersistedBinding(persisted.librarySelectionAuthority);
  return {
    pythonCommand: managedPythonCommand(projectRoot),
    autoStartBackend: persisted.autoStartBackend,
    libraryConfigured: binding !== null,
    defaultLibraryDir: binding?.canonicalRoot ?? suggestedLibraryDir,
    defaultDbPath: binding?.dbPath ?? resolveLibraryDbPath(suggestedLibraryDir),
  };
}

export async function loadDesktopSettings(projectRoot: string): Promise<DesktopSettings> {
  return toDesktopSettings(projectRoot, await readPersistedSettings());
}

export async function loadApprovedDesktopLibraryBinding(): Promise<
  ApprovedDesktopLibraryBinding | null
> {
  const persisted = await readPersistedSettings();
  return normalizePersistedBinding(persisted.librarySelectionAuthority);
}

function normalizeSettingsUpdate(rawUpdate: unknown): DesktopSettingsUpdate {
  if (rawUpdate === null || typeof rawUpdate !== "object" || Array.isArray(rawUpdate)) {
    throw new Error("MemoLens rejected an invalid desktop settings update.");
  }
  const record = rawUpdate as Record<string, unknown>;
  const keys = Object.keys(record);
  if (keys.some((key) => key !== "autoStartBackend")) {
    throw new Error("MemoLens rejected renderer-controlled library or runtime settings.");
  }
  if (typeof record.autoStartBackend !== "boolean") {
    throw new Error("MemoLens requires autoStartBackend to be a boolean.");
  }
  return { autoStartBackend: record.autoStartBackend };
}

export async function saveDesktopSettings(
  projectRoot: string,
  rawUpdate: unknown,
): Promise<DesktopSettings> {
  const update = normalizeSettingsUpdate(rawUpdate);
  return mutatePersistedSettings(async () => {
    const current = await readPersistedSettings();
    const next = { ...current, autoStartBackend: update.autoStartBackend };
    await writePersistedSettings(next);
    return toDesktopSettings(projectRoot, next);
  });
}

export async function commitDesktopLibrarySelection(
  projectRoot: string,
  binding: ApprovedDesktopLibraryBinding,
): Promise<DesktopSettings> {
  const canonicalRoot = normalizeLibraryIdentity(binding.canonicalRoot);
  const dbPath = resolve(binding.dbPath);
  if (
    canonicalRoot !== binding.canonicalRoot
    || dbPath !== resolveLibraryDbPath(canonicalRoot)
    || typeof binding.device !== "string"
    || typeof binding.inode !== "string"
    || binding.device.length === 0
    || binding.inode.length === 0
  ) {
    throw new Error("MemoLens rejected an unverified desktop library binding.");
  }
  return mutatePersistedSettings(async () => {
    const current = await readPersistedSettings();
    const next: PersistedDesktopSettings = {
      ...current,
      librarySelectionAuthority: {
        schemaVersion: 1,
        canonicalRoot,
        dbPath,
        device: binding.device,
        inode: binding.inode,
      },
    };
    await writePersistedSettings(next);
    return toDesktopSettings(projectRoot, next);
  });
}

/** Called only by the bootstrap coordinator after Core commit and active proof. */
export async function commitBootstrapLibraryProjection(
  projectRoot: string,
  binding: ApprovedDesktopLibraryBinding,
): Promise<DesktopSettings> {
  // Validate using the same closed native binding contract before either write.
  const approved = normalizePersistedBinding({ schemaVersion: 1, ...binding });
  if (approved === null) {
    throw new Error("MemoLens rejected an unverified bootstrap Library projection.");
  }
  return mutatePersistedSettings(async () => {
    const backendPath = join(getCanonicalAppStateDir(), "backend-settings.json");
    let preferences: Record<string, unknown> = {};
    try {
      const parsed: unknown = JSON.parse(await readFile(backendPath, "utf8"));
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("MemoLens rejected malformed backend settings during bootstrap repair.");
      }
      preferences = parsed as Record<string, unknown>;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
    await writeSettingsProjection(backendPath, {
      ...preferences,
      image_library_dir: approved.canonicalRoot,
      db_path: approved.dbPath,
    });
    const current = await readPersistedSettings();
    const next: PersistedDesktopSettings = {
      ...current,
      librarySelectionAuthority: { schemaVersion: 1, ...approved },
    };
    await writePersistedSettings(next);
    return toDesktopSettings(projectRoot, next);
  });
}
