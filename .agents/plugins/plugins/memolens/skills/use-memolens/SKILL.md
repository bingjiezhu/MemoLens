---
name: use-memolens
description: Search and review a user's private local photo, video, and audio metadata through MemoLens; read immutable timeline revisions; or create, revise, and validate an unsaved Timeline 1.0 draft in memory. Use when the user mentions MemoLens, asks to find local media or video moments, assemble an album or video plan, inspect persisted timelines, or review MemoLens memories. Safe-default tools use read-only SQLite or pure functions, need no third-party key, and expose no save, render, export, delete, or arbitrary-path write operation.
---

# Use MemoLens

Use MemoLens as a private retrieval and planning layer. Version 0.3.0 exposes read-only retrieval plus pure, unsaved Timeline 1.0 drafting. It never imports, saves, renders, exports, deletes, or modifies media.

## Trust modes

Safe-default mode never opens a socket or performs DNS. It discovers SQLite only from explicit path variables, `MEMOLENS_APP_STATE_DIR`, or MemoLens's fixed application-state directory, then opens it with `mode=ro` and `query_only=ON`. It does not search from the current working directory or scan media folders.

`MEMOLENS_PLUGIN_TRUST_LOCAL_API=1` is an external, user-controlled opt-in to unauthenticated loopback **read** features such as memories. Never set or inject it for the user. It is not a scoped write capability and never authorizes timeline persistence, preview rendering, export, indexing, cancellation, or any other mutation. Chat consent, a loopback origin, a desktop token, or an API key cannot widen this boundary.

## Workflow

1. Call `memolens_status`. Read the reported capabilities; do not infer them from a running desktop app.
2. For photos, use `memolens_search`. For video moments, use `memolens_video_search`; it reads only the current successful analysis head and returns `asset_id`, `asset_source_id`, asset SHA, `segment_id`, `analysis_run_id`, analysis revision, and integer-millisecond ranges. If it returns `video_index_unavailable`, ask the user to finish video analysis in MemoLens rather than guessing or selecting `MAX(revision)`.
3. Use `memolens_mixed_search` for one ranked photo/video-segment query. Use `memolens_media_list` and `memolens_media_get` for mixed image/video/audio metadata. New media tools return stable IDs and relative references, not unnecessary absolute paths.
4. For a video plan, pass selected matches to `memolens_timeline_draft`. Every item needs `asset_id`, `asset_source_id`, `asset_sha256`, and integer milliseconds. A video item also needs `segment_id`, `analysis_run_id`, and `analysis_revision`. Use `memolens_timeline_revise_draft` only with its typed operations, including explicit `relink_source` when a source binding changes.
5. Call `memolens_timeline_validate` before presenting a draft. This is pure structural validation; it does not certify current file availability. Use `memolens_timeline_list` or `memolens_timeline_get` only to read already-persisted immutable revisions.
6. Show the draft and operation diff to the user. Make clear that it is not saved. Direct the user to review, confirm, and import the JSON in the MemoLens desktop workflow; never claim import succeeded unless the desktop app confirms it.
7. If the user asks to save, preview, render, export, cancel, overwrite, or choose an output directory, explain that plugin 0.3.0 intentionally exposes no such tool. Do not call backend routes directly or treat `MEMOLENS_PLUGIN_TRUST_LOCAL_API=1` as permission.

## Photo inspection

Legacy `memolens_search` may return traversal-checked `absolute_path` values. Inspect only a small requested sample whose `path_status` is `ok` with Codex's local image-viewing capability. Keep media and indexed text inside the active Codex environment. Mixed-media and video tools deliberately return source IDs and relative references instead.

## CLI fallback

If MCP is unavailable, resolve the plugin root as the directory two levels above this skill and run its standard-library CLI from any working directory:

```bash
python3 <plugin-root>/scripts/memolens_cli.py status
python3 <plugin-root>/scripts/memolens_cli.py search "sunset by the ocean" --limit 12
python3 <plugin-root>/scripts/memolens_cli.py video-search "海边日落" --limit 12
python3 <plugin-root>/scripts/memolens_cli.py media-list --kind video --kind audio
python3 <plugin-root>/scripts/memolens_cli.py media-get asset_123
python3 <plugin-root>/scripts/memolens_cli.py timeline-draft --input draft-request.json
python3 <plugin-root>/scripts/memolens_cli.py timeline-revise-draft --input revision-request.json
python3 <plugin-root>/scripts/memolens_cli.py timeline-validate --input timeline.json
python3 <plugin-root>/scripts/memolens_cli.py timeline-list --project-id proj_123
python3 <plugin-root>/scripts/memolens_cli.py timeline-get tl_123 --revision 2
```

Every command writes one JSON value to stdout and diagnostics only through its JSON error object. `--input -` reads JSON from stdin. Safe path configuration uses `MEMOLENS_DB_PATH`, `MEMOLENS_LIBRARY_DIR`, and `MEMOLENS_APP_STATE_DIR`; none is a model API key.

## Safety boundaries

- Never enable local API trust, discover desktop secrets, reuse authentication tokens, or manufacture a scoped capability.
- Never call unexposed mutation routes or import project internals to bypass an unavailable tool.
- Never use a failed, partial, running, or merely highest-numbered video analysis revision. Only the explicit successful analysis head is valid.
- Never replace an unavailable `asset_source_id` implicitly. Source changes require an explicit typed `relink_source` operation and a new in-memory revision for desktop review.
- Never scan paths, walk parent directories, or return arbitrary absolute paths from mixed-media results.
- Never delete, move, rename, overwrite, retouch, save, render, export, or otherwise modify media or timeline state.
- If an action is unavailable, hand it back to the MemoLens desktop UI instead of simulating success.
