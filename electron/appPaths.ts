import { homedir } from "node:os";
import { join, resolve } from "node:path";

export function getCanonicalAppStateDir(): string {
  const override = process.env.MEMOLENS_APP_STATE_DIR?.trim();
  if (override) {
    return resolve(override);
  }
  return join(homedir(), "Library", "Application Support", "MemoLens");
}
