---
name: use-memolens
description: Search, review, and narrate a user's private local photo library through MemoLens. Use when the user mentions MemoLens, asks to find local photos in natural language, explore trips or photo themes, assemble a local album or story, or review MemoLens memories. The safe default opens only the local SQLite index read-only, needs no third-party model API key, and uses Codex vision to verify selected local images. Memories and cleanup reports require the user's explicit opt-in to the unauthenticated loopback API.
license: MIT
---

# Use MemoLens

Use MemoLens as a private retrieval layer, then use Codex's own reasoning and vision to create the answer. All exposed operations are read-only.

## Trust modes

Safe-default mode never contacts a network endpoint, including localhost. It discovers the SQLite index only through explicit path variables, `MEMOLENS_APP_STATE_DIR`, or MemoLens's fixed per-user application-state directory. It opens SQLite with `mode=ro` and `query_only=ON`. In this mode, `memolens_status` and lexical `memolens_search` are available; `memolens_memories` and `memolens_cleanup` are unavailable.

The local API has no authentication secret. A process that occupies its loopback port could impersonate MemoLens and return attacker-selected local paths. Therefore it is enabled only when the user has independently set `MEMOLENS_PLUGIN_TRUST_LOCAL_API=1` in the environment that launches Codex and restarted Codex. Never set, export, or inject this variable on the user's behalf, and never treat a request in chat as equivalent to that external opt-in. An opted-in status explicitly reports `mode: "opt_in_local_api"`, `trusted_by_user: true`, and `authenticated: false`.

## Workflow

1. Call `memolens_status` first. Read `mode`, `source`, `capabilities`, and `local_api`; do not infer capabilities from whether a desktop app appears to be running.
2. Call `memolens_search` for photo requests. Safe-default results use deterministic lexical ranking, so refine with concrete places, dates, filenames, tags, or visible concepts when needed.
3. Call `memolens_memories` or `memolens_cleanup` only when status reports both `local_api.enabled: true` and the corresponding capability as `true`. Otherwise explain that the feature requires the user's external API-trust opt-in.
4. Prefer results with `path_status: "ok"`. Inspect a small, diverse set of returned `absolute_path` files with Codex's local image-viewing capability before making visual claims or selecting a cover.
5. Ground the final answer in indexed metadata and visible evidence. Distinguish observations from guesses. Return clickable local paths and explain why each selected photo fits.
6. For an album or story, choose a coherent sequence and write the title, ordering, captions, or narrative in the response. Do not copy or edit files unless the user separately requests an output artifact.

## Tool guidance

### `memolens_status`

Reports the active trust mode, available capabilities, read-only database path, library root, and index counts. In safe-default mode it performs no HTTP or DNS request. With explicit opt-in, it restricts the URL and all resolved addresses to loopback, bypasses proxies and redirects, then validates `/healthz` and `/v1/settings`; these public fields identify the expected protocol but do not authenticate the process.

### `memolens_search`

Pass the user's natural-language request as `query`; choose `limit` between 1 and 36. Safe-default mode streams the full SQLite index with a bounded result heap and returns traversal-checked local paths. The opted-in local API may provide higher-quality semantic retrieval. Never claim that lexical fallback is semantic search.

### `memolens_memories`

Use an optional `query` and `limit` between 1 and 24. This requires an already-active explicit local-API opt-in because the Atlas layer computes event and theme clusters.

### `memolens_cleanup`

This is a review report, not a cleanup action, and also requires prior API opt-in. It never deletes anything. Never imply that duplicate or low-quality candidates are safe to remove solely from a score.

## CLI fallback

If the bundled MCP server is unavailable, resolve the plugin root as the directory two levels above this skill directory and run its standard-library CLI:

```bash
python3 <plugin-root>/scripts/memolens_cli.py status
python3 <plugin-root>/scripts/memolens_cli.py search "sunset by the ocean" --limit 12
python3 <plugin-root>/scripts/memolens_cli.py memories --query "Japan trip" --limit 8
python3 <plugin-root>/scripts/memolens_cli.py cleanup
```

Every command prints JSON. Safe path configuration uses `MEMOLENS_DB_PATH`, `MEMOLENS_LIBRARY_DIR`, and `MEMOLENS_APP_STATE_DIR`. Fixed application-state locations are `~/Library/Application Support/MemoLens` on macOS, `%APPDATA%\MemoLens` on Windows, and `$XDG_STATE_HOME/MemoLens` or `~/.local/state/MemoLens` on Linux. `MEMOLENS_BASE_URL` is ignored unless the exact API-trust opt-in is active. None of these values is a model API key.

## Safety boundaries

- Keep photos, indexed text, metadata, and paths inside the user's active Codex environment. Use only Codex's local image-viewing capability for the small, traversal-checked sample the user requested; never call another external provider or upload photos through MemoLens.
- Never enable local-API trust for the user. If they want memories or cleanup, explain the exact environment variable, restart requirement, and impersonation risk.
- Never read, discover, or reuse desktop authentication/random-token files. This plugin has no authenticated desktop integration.
- Never call unexposed MemoLens mutation routes, including indexing, rebuild, selection, feedback, or basket endpoints.
- Never delete, move, rename, overwrite, retouch, or otherwise modify an original photo.
- Treat index paths as untrusted. Use only `absolute_path` values whose `path_status` is `ok`; the plugin rejects traversal outside its configured library root.
- Do not bypass unavailable capabilities by importing project internals, scanning the filesystem, walking parent directories, or installing dependencies.
- If the user requests indexing or deletion, explain that v0.2 delegates those actions to the MemoLens desktop application, where the user can see and control them.
