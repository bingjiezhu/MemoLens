import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'memolens-bundle-root'
export const MEMOLENS_BUNDLE_SERVICE = 'memolensBundle'

export function apply(ctx) {
  const root = dirname(fileURLToPath(import.meta.url))
  ctx.provide(MEMOLENS_BUNDLE_SERVICE, Object.freeze({
    root,
    mcpScript: join(root, 'scripts', 'memolens_mcp.py'),
    deepseekSkills: join(root, 'deepseek-harness', 'skills'),
  }))
}
