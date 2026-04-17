import { homedir } from "node:os";
import { join } from "node:path";

export function getCanonicalAppStateDir(): string {
  return join(homedir(), "Library", "Application Support", "MemoLens");
}
