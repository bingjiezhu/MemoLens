import { join } from "node:path";


const MEMOLENS_PLUGIN_PATH = "/memolens";


export function buildMemoLensCodexUrl(projectRoot: string): string {
  const url = new URL(`codex://plugins${MEMOLENS_PLUGIN_PATH}`);
  url.searchParams.set(
    "marketplacePath",
    join(projectRoot, ".agents", "plugins", "marketplace.json"),
  );
  return url.toString();
}
