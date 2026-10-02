import { isAbsolute, join, resolve } from "node:path";


const MEMOLENS_PLUGIN_PATH = "/memolens";

function canonicalProjectRoot(projectRoot: string): string {
  if (
    typeof projectRoot !== "string"
    || !isAbsolute(projectRoot)
    || projectRoot.includes("\0")
    || resolve(projectRoot) !== projectRoot
  ) {
    throw new Error("MemoLens requires a canonical absolute local plugin root.");
  }
  return projectRoot;
}

export function isMemoLensLocalPluginUrl(targetUrl: string, projectRoot: string): boolean {
  try {
    const canonicalRoot = canonicalProjectRoot(projectRoot);
    const url = new URL(targetUrl);
    return url.protocol === "codex:"
      && url.hostname === "plugins"
      && url.port === ""
      && url.username === ""
      && url.password === ""
      && url.pathname === MEMOLENS_PLUGIN_PATH
      && url.hash === ""
      && [...url.searchParams.keys()].length === 1
      && url.searchParams.get("marketplacePath")
        === join(canonicalRoot, ".agents", "plugins", "marketplace.json");
  } catch {
    return false;
  }
}

export async function dispatchMemoLensLocalPluginUrl(
  targetUrl: string,
  projectRoot: string,
  dispatch: (validatedTargetUrl: string) => Promise<unknown> | unknown,
): Promise<void> {
  if (!isMemoLensLocalPluginUrl(targetUrl, projectRoot)) {
    throw new Error("MemoLens refused an invalid local plugin installation target.");
  }
  await dispatch(targetUrl);
}


export function buildMemoLensCodexUrl(projectRoot: string): string {
  const canonicalRoot = canonicalProjectRoot(projectRoot);
  const url = new URL(`codex://plugins${MEMOLENS_PLUGIN_PATH}`);
  url.searchParams.set(
    "marketplacePath",
    join(canonicalRoot, ".agents", "plugins", "marketplace.json"),
  );
  const targetUrl = url.toString();
  if (!isMemoLensLocalPluginUrl(targetUrl, canonicalRoot)) {
    throw new Error("MemoLens refused an invalid local plugin installation target.");
  }
  return targetUrl;
}
