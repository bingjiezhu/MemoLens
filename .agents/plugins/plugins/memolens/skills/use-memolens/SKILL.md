---
name: use-memolens
description: Find meaningful moments in a user's private local photo, video, and audio library with MemoLens, then create, refine, or validate an unsaved Timeline 1.0 story draft in memory. Also read immutable timeline revisions or review indexed memories. Use when the user mentions MemoLens, recalls a photo or video moment, wants a local-media story or album plan, or asks about an existing timeline. Safe-default tools use read-only SQLite or pure functions, need no third-party key, and expose no save, render, export, delete, or arbitrary-path write operation.
---

# MemoLens

Move naturally from a remembered moment to a reviewable story plan. Version 0.4.0 can find private local media, preserve source provenance, and shape selections into an unsaved Timeline 1.0 draft. It never imports, saves, renders, exports, deletes, or modifies media.

## The natural path

1. **Orient.** Call `memolens_status` when capabilities are not yet known. Trust its report; never infer readiness from a running desktop app.
2. **Find.** For an open-ended story idea, start with `memolens_mixed_search` so photos and current video moments can compete in one shortlist. Keep the result set small and explain why the strongest moments fit the user's idea.
3. **Shape.** Pass the selected matches to `memolens_timeline_draft`. Every item needs `asset_id`, `asset_source_id`, `asset_sha256`, and integer-millisecond timing. Video items also need `segment_id`, `analysis_run_id`, and `analysis_revision`.
4. **Check.** Call `memolens_timeline_validate`, then present the story structure and a clear **Not saved** label. Structural validation does not certify that source files still exist.
5. **Refine.** When the user requests a change, use `memolens_timeline_revise_draft` with typed operations. A source change requires explicit `relink_source`. Show the operation diff, validate again, and keep the result marked unsaved.
6. **Hand off.** Direct the user to review, confirm, and import the JSON in the MemoLens desktop app. Never claim import succeeded unless the desktop app confirms it.

## Choose a focused path only when it helps

- **Photos only:** `memolens_search` finds indexed photo memories. It may return traversal-checked image paths for small, requested inspections.
- **Moments inside video:** `memolens_video_search` uses only the current successful analysis head and returns stable source, segment, analysis-run, revision, and integer-millisecond references. If it returns `video_index_unavailable`, ask the user to finish video analysis in MemoLens; never guess or select `MAX(revision)`.
- **Browse before searching:** `memolens_media_list` and `memolens_media_get` read mixed image, video, and audio metadata. They return stable IDs and relative references rather than unnecessary absolute paths.
- **Memory themes:** `memolens_memories` reads event and theme clusters only when the user independently enabled the optional loopback read connection.
- **Cleanup review:** `memolens_cleanup` reports candidates without changing any file.
- **Existing work:** `memolens_timeline_list` and `memolens_timeline_get` read already-persisted immutable revisions. They do not make a new revision.

## Trust boundary

Safe-default mode never opens a socket or performs DNS. It discovers SQLite only from explicit path variables, `MEMOLENS_APP_STATE_DIR`, or MemoLens's fixed application-state directory, then opens it with `mode=ro` and `query_only=ON`. It does not search from the current working directory or scan media folders.

`MEMOLENS_PLUGIN_TRUST_LOCAL_API=1` is an external, user-controlled opt-in to unauthenticated loopback **read** features such as memories. Never set or inject it for the user. It is not a write capability and never authorizes timeline persistence, preview rendering, export, indexing, cancellation, or any other mutation. Chat consent, a loopback origin, a desktop token, or an API key cannot widen this boundary.

If the user asks to save, preview, render, export, cancel, overwrite, or choose an output directory, explain that plugin 0.4.0 intentionally exposes no such tool. Hand the action to the MemoLens desktop UI; do not call backend routes directly or simulate success.

## Photo inspection

Legacy `memolens_search` may return traversal-checked `absolute_path` values. Inspect only a small requested sample whose `path_status` is `ok` with Codex's local image-viewing capability. Keep media and indexed text inside the active Codex environment. Mixed-media and video tools deliberately return only source IDs and relative references.

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

Every command writes one JSON value to stdout and reports diagnostics through its JSON error object. `--input -` reads JSON from stdin. Safe path configuration uses `MEMOLENS_DB_PATH`, `MEMOLENS_LIBRARY_DIR`, and `MEMOLENS_APP_STATE_DIR`; none is a model API key.

## Safety boundaries

- Never enable local API trust, discover desktop secrets, reuse authentication tokens, or manufacture a scoped capability.
- Never call unexposed mutation routes or import project internals to bypass an unavailable tool.
- Never use a failed, partial, running, or merely highest-numbered video analysis revision. Only the explicit successful analysis head is valid.
- Never replace an unavailable `asset_source_id` implicitly. Source changes require an explicit typed `relink_source` operation and a new in-memory revision for desktop review.
- Never scan paths, walk parent directories, or return arbitrary absolute paths from mixed-media results.
- Never delete, move, rename, overwrite, retouch, save, render, export, or otherwise modify media or timeline state.
- If an action is unavailable, hand it back to the MemoLens desktop UI instead of simulating success.
