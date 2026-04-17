import fs from "node:fs";
import path from "node:path";

import type { ResolvedImageBatch } from "./types.js";

export function resolveImagePath(root: string, relativePath: string): string {
  const normalizedRoot = path.resolve(root);
  const absolute = path.resolve(normalizedRoot, relativePath);
  const relativeToRoot = path.relative(normalizedRoot, absolute);

  if (
    !relativeToRoot ||
    relativeToRoot.startsWith("..") ||
    path.isAbsolute(relativeToRoot)
  ) {
    throw new Error("Resolved image path escapes IMAGE_LIBRARY_DIR.");
  }

  if (!fs.existsSync(absolute) || !fs.statSync(absolute).isFile()) {
    throw new Error(`Image file does not exist: ${absolute}`);
  }

  return absolute;
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
