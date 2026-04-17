import { access, mkdir, readFile, writeFile } from "node:fs/promises";
import { constants } from "node:fs";
import { dirname, join, resolve } from "node:path";

import type { DesktopSettings } from "../src/query/types.js";
import { getCanonicalAppStateDir } from "./appPaths.js";

export const DEFAULT_BACKEND_URL = "http://127.0.0.1:5519";

function getSettingsPath(): string {
  return join(getCanonicalAppStateDir(), "desktop-settings.json");
}

async function pathExists(path: string): Promise<boolean> {
  try {
    await access(path, constants.F_OK);
    return true;
  } catch {
    return false;
  }
}

async function getDefaultPythonCommand(projectRoot: string): Promise<string> {
  const venvPython = join(projectRoot, ".venv", "bin", "python");
  return (await pathExists(venvPython)) ? venvPython : "python3";
}

function normalizeOptionalPath(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  if (trimmed.length === 0) {
    return null;
  }
  return resolve(trimmed);
}

function normalizeUrl(value: unknown, fallback: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    return fallback;
  }
  const normalized = value.trim().replace(/\/+$/, "");
  if (
    normalized === "http://127.0.0.1:5000" ||
    normalized === "http://localhost:5000"
  ) {
    return fallback;
  }
  return normalized;
}

function normalizePythonCommand(value: unknown, fallback: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    return fallback;
  }
  return value.trim();
}

function normalizeSettings(
  rawValue: unknown,
  defaults: DesktopSettings,
): DesktopSettings {
  const raw =
    rawValue !== null && typeof rawValue === "object"
      ? (rawValue as Partial<DesktopSettings>)
      : {};
  const defaultLibraryDir = normalizeOptionalPath(raw.defaultLibraryDir) ?? defaults.defaultLibraryDir;
  const defaultDbPath = normalizeOptionalPath(raw.defaultDbPath) ?? defaults.defaultDbPath;

  return {
    backendUrl: normalizeUrl(raw.backendUrl, defaults.backendUrl),
    pythonCommand: normalizePythonCommand(raw.pythonCommand, defaults.pythonCommand),
    autoStartBackend:
      typeof raw.autoStartBackend === "boolean"
        ? raw.autoStartBackend
        : defaults.autoStartBackend,
    defaultLibraryDir,
    defaultDbPath,
  };
}

export async function loadDesktopSettings(projectRoot: string): Promise<DesktopSettings> {
  const defaultLibraryDir = resolve(projectRoot, "local-photo-library");
  const appStateDir = getCanonicalAppStateDir();
  const defaults: DesktopSettings = {
    backendUrl: DEFAULT_BACKEND_URL,
    pythonCommand: await getDefaultPythonCommand(projectRoot),
    autoStartBackend: true,
    defaultLibraryDir,
    defaultDbPath: join(appStateDir, "storage", "photo_index.db"),
  };

  try {
    const content = await readFile(getSettingsPath(), "utf-8");
    return normalizeSettings(JSON.parse(content), defaults);
  } catch {
    return defaults;
  }
}

export async function saveDesktopSettings(
  projectRoot: string,
  rawSettings: DesktopSettings,
): Promise<DesktopSettings> {
  const normalized = normalizeSettings(rawSettings, await loadDesktopSettings(projectRoot));
  const settingsPath = getSettingsPath();
  await mkdir(dirname(settingsPath), { recursive: true });
  await writeFile(settingsPath, `${JSON.stringify(normalized, null, 2)}\n`, "utf-8");
  return normalized;
}
