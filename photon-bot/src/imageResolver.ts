import fs from "node:fs";
import path from "node:path";

import type { ResolvedImageBatch } from "./types.js";

/**
 * Resolve a backend-supplied relative image path without allowing symlinks or
 * non-regular filesystem objects anywhere below the configured library root.
 */
export function resolveImagePath(root: string, relativePath: string): string {
  const normalizedRelativePath = path.normalize(relativePath);
  if (
    !relativePath.trim() ||
    path.isAbsolute(relativePath) ||
    normalizedRelativePath !== relativePath ||
    normalizedRelativePath === "."
  ) {
    throw new Error("Image path must be a non-empty relative path.");
  }

  const { configuredRoot, realRoot } = resolveTrustedRoot(root);
  const lexicalCandidate = path.resolve(configuredRoot, normalizedRelativePath);
  const relativeToRoot = descendantRelativePath(configuredRoot, lexicalCandidate);

  return resolveSymlinkFreeDescendant(realRoot, relativeToRoot);
}

/**
 * Revalidate an already-resolved path immediately before an egress consumer
 * opens it. This accepts either the configured-root spelling or its realpath.
 */
export function revalidateResolvedImagePath(root: string, imagePath: string): string {
  if (!path.isAbsolute(imagePath)) {
    throw new Error("Resolved image path must be absolute.");
  }

  const { configuredRoot, realRoot } = resolveTrustedRoot(root);
  const absolute = path.resolve(imagePath);
  let relativeToRoot: string;

  try {
    relativeToRoot = descendantRelativePath(realRoot, absolute);
  } catch {
    relativeToRoot = descendantRelativePath(configuredRoot, absolute);
  }

  return resolveSymlinkFreeDescendant(realRoot, relativeToRoot);
}

export function resolveImageBatch(
  root: string,
  relativePaths: readonly string[],
  limit: number,
): ResolvedImageBatch {
  const imagePaths: string[] = [];
  const missingRelativePaths: string[] = [];
  let consumedCount = 0;

  for (const relativePath of relativePaths) {
    if (imagePaths.length >= limit) {
      break;
    }

    consumedCount += 1;
    try {
      imagePaths.push(resolveImagePath(root, relativePath));
    } catch {
      missingRelativePaths.push(relativePath);
    }
  }

  return {
    imagePaths,
    missingRelativePaths,
    consumedCount,
  };
}

function resolveTrustedRoot(root: string): {
  configuredRoot: string;
  realRoot: string;
} {
  const configuredRoot = path.resolve(root);
  const rootStat = fs.lstatSync(configuredRoot);
  if (rootStat.isSymbolicLink() || !rootStat.isDirectory()) {
    throw new Error("IMAGE_LIBRARY_DIR must be a real directory, not a symlink.");
  }

  const realRoot = fs.realpathSync.native(configuredRoot);
  const realRootStat = fs.lstatSync(realRoot);
  if (realRootStat.isSymbolicLink() || !realRootStat.isDirectory()) {
    throw new Error("IMAGE_LIBRARY_DIR did not resolve to a real directory.");
  }

  return { configuredRoot, realRoot };
}

function descendantRelativePath(root: string, candidate: string): string {
  const relative = path.relative(root, candidate);
  if (
    !relative ||
    relative === ".." ||
    relative.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relative)
  ) {
    throw new Error("Resolved image path escapes IMAGE_LIBRARY_DIR.");
  }
  return relative;
}

function resolveSymlinkFreeDescendant(realRoot: string, relativePath: string): string {
  const components = relativePath.split(path.sep).filter(Boolean);
  if (components.length === 0) {
    throw new Error("Resolved image path must identify a file below IMAGE_LIBRARY_DIR.");
  }

  let current = realRoot;
  for (const [index, component] of components.entries()) {
    current = path.join(current, component);
    const stat = fs.lstatSync(current);
    if (stat.isSymbolicLink()) {
      throw new Error("Resolved image path contains a symbolic link.");
    }

    const isFinal = index === components.length - 1;
    if (!isFinal && !stat.isDirectory()) {
      throw new Error("Resolved image path contains a non-directory component.");
    }
    if (isFinal && !stat.isFile()) {
      throw new Error("Resolved image path is not a regular file.");
    }
  }

  const realCandidate = fs.realpathSync.native(current);
  descendantRelativePath(realRoot, realCandidate);
  return realCandidate;
}
