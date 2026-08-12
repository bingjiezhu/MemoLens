export interface PersistedVideoSession {
  projectId: string;
  timelineId: string | null;
  timelineRevision: number | null;
}

export interface VideoSessionStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

const SCOPE_SEPARATOR = "\u001f";
const SESSION_KEY_PREFIX = "memolens.video.session.";

export function createVideoScopeKey(
  imageLibraryDir: string | null | undefined,
  dbPath: string | null | undefined,
): string {
  return `${imageLibraryDir?.trim() ?? ""}${SCOPE_SEPARATOR}${dbPath?.trim() ?? ""}`;
}

function scopeFingerprint(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

export function videoSessionStorageKey(scopeKey: string): string {
  return `${SESSION_KEY_PREFIX}${scopeFingerprint(scopeKey)}`;
}

function hasScopedLibrary(scopeKey: string): boolean {
  return scopeKey.split(SCOPE_SEPARATOR).some((part) => part.trim().length > 0);
}

export function readPersistedVideoSession(
  storage: VideoSessionStorage,
  scopeKey: string,
): PersistedVideoSession | null {
  if (!hasScopedLibrary(scopeKey)) return null;
  try {
    const parsed = JSON.parse(
      storage.getItem(videoSessionStorageKey(scopeKey)) ?? "null",
    ) as Partial<PersistedVideoSession> | null;
    if (!parsed || typeof parsed.projectId !== "string" || !parsed.projectId) {
      return null;
    }
    return {
      projectId: parsed.projectId,
      timelineId: typeof parsed.timelineId === "string" ? parsed.timelineId : null,
      timelineRevision: typeof parsed.timelineRevision === "number"
        ? parsed.timelineRevision
        : null,
    };
  } catch {
    return null;
  }
}

export function persistVideoSession(
  storage: VideoSessionStorage,
  scopeKey: string,
  session: PersistedVideoSession,
): void {
  try {
    storage.setItem(videoSessionStorageKey(scopeKey), JSON.stringify(session));
  } catch {
    // SQLite remains authoritative when browser storage is unavailable.
  }
}
