---
name: use-memolens
description: Use a user's confirmed Creator Memory to find meaningful moments in their private local photo, video, and audio library, review the non-destructive Media Inbox, and create, refine, or validate an unsaved Timeline 1.0 story draft. Use when the user mentions MemoLens, wants to recall or organize local media, plan creator content, review an inbox, or shape a story. Safe-default tools are read-only, need no third-party key, and expose no review/profile write, save, render, export, delete, or arbitrary-path operation.
---

# MemoLens

Move naturally from creator context or an unreviewed memory to a reviewable story plan. Version 0.5.0 reads only confirmed preferences, finds private local media, preserves source provenance, and shapes selections into an unsaved Timeline 1.0 draft. It never writes review state or Creator Memory, imports, saves, renders, exports, deletes, or modifies media.

## The natural path

1. **Orient.** Call `memolens_status` when capabilities are not yet known. Trust its report; never infer readiness from a running desktop app.
2. **Read confirmed context.** Call `memolens_creator_context`. Apply only fields present in its bounded `profile`, say which preferences influenced the plan, and never invent a missing preference. `confirmed_only: true` and `hidden_inference: false` are hard policy, not marketing text.
3. **Find or review.** Use `memolens_mixed_search` for an open-ended story so photos and current video moments compete in one shortlist. Use `memolens_inbox_list` when the user wants to triage incoming media or focus on `inbox`, `kept`, or `archived` state. Keep results small and explain fit.
4. **Shape.** Pass selected match provenance to `memolens_timeline_draft`. Every item needs `asset_id`, `asset_source_id`, `asset_sha256`, and integer-millisecond timing. Video items also need `segment_id`, `analysis_run_id`, and `analysis_revision`.
5. **Check and refine.** Call `memolens_timeline_validate`, keep a clear **Not saved** label, and use `memolens_timeline_revise_draft` only for requested typed changes. A source change requires explicit `relink_source`.
6. **Hand off.** Direct the user to review and confirm Inbox decisions, Creator Memory edits, and timeline import in the MemoLens app. Never claim a write succeeded unless the App confirms it.

## Choose a focused path only when it helps

- **Photos only:** `memolens_search` finds indexed photo memories. It may return traversal-checked image paths for small, requested inspections.
- **Creator preferences:** `memolens_creator_context` returns the latest `default` profile revision, content hash, confirmed source, and aggregate evidence counts. It strips unknown profile fields and never returns evidence payloads, raw prompts, or chats.
- **Media review:** `memolens_inbox_list` returns stable source/hash/timing provenance and the current review snapshot. Codex may recommend Keep, Archive, Favorite, or Ready, but must present that as a suggestion. The App owns the final diff and confirmation; Archive never changes the original file.
- **Moments inside video:** `memolens_video_search` uses only the current successful analysis head and returns stable source, segment, analysis-run, revision, and integer-millisecond references. If it returns `video_index_unavailable`, ask the user to finish video analysis in MemoLens; never guess or select `MAX(revision)`.
- **Browse before searching:** `memolens_media_list` reads non-archived image, video, and audio metadata by default. Use explicit `memolens_media_get` or `memolens_inbox_list(state="archived")` only when the user asks to inspect archived media. Both return stable IDs and relative references rather than unnecessary absolute paths.
- **Memory themes:** `memolens_memories` reads event and theme clusters only when the user independently enabled the optional loopback read connection.
- **Cleanup review:** `memolens_cleanup` reports candidates without changing any file.
- **Existing work:** `memolens_timeline_list` and `memolens_timeline_get` read already-persisted immutable revisions. They do not make a new revision.

## Trust boundary

Safe-default mode never opens a socket or performs DNS. It discovers SQLite only from explicit path variables, `MEMOLENS_APP_STATE_DIR`, or MemoLens's fixed application-state directory. It requires an existing WAL-mode database, copies a stable DB/WAL snapshot into a private temporary directory, validates the complete copied WAL and its checksums, and queries only that private snapshot with `mode=ro`, `query_only=ON`, foreign keys enabled, and `busy_timeout=5000`. The original DB, WAL, SHM, and directory entries remain byte-for-byte untouched. It does not search from the current working directory, scan media folders, or repair SQLite state.

`MEMOLENS_PLUGIN_TRUST_LOCAL_API=1` is an external, user-controlled opt-in to unauthenticated loopback **read** features such as memories. Never set or inject it for the user. It is not a write capability and never authorizes timeline persistence, preview rendering, export, indexing, cancellation, or any other mutation. Chat consent, a loopback origin, a desktop token, or an API key cannot widen this boundary.

If the user asks to change Inbox state or Creator Memory, save, preview, render, export, cancel, overwrite, or choose an output directory, explain that plugin 0.5.0 intentionally exposes no such tool. Hand the action to the MemoLens app; do not call backend routes directly or simulate success.

## Photo inspection

Legacy `memolens_search` may return traversal-checked `absolute_path` values. Inspect only a small requested sample whose `path_status` is `ok` with Codex's local image-viewing capability. Keep media and indexed text inside the active Codex environment. `memolens_mixed_search`, `memolens_inbox_list`, Creator Memory, and video tools deliberately return no absolute path. Do not derive one from a relative reference.

## CLI fallback

If MCP is unavailable, resolve the plugin root as the directory two levels above this skill and run its standard-library CLI from any working directory:

```bash
python3 <plugin-root>/scripts/memolens_cli.py status
python3 <plugin-root>/scripts/memolens_cli.py creator-context
python3 <plugin-root>/scripts/memolens_cli.py inbox-list --state inbox --kind image --kind video
python3 <plugin-root>/scripts/memolens_cli.py search "sunset by the ocean" --limit 12
python3 <plugin-root>/scripts/memolens_cli.py mixed-search "海边日落" --limit 12
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
- Never infer preferences from raw chat, dwell time, one click, or an unconfirmed local observation. Apply only the returned confirmed profile snapshot.
- Never turn an Inbox suggestion into a claimed decision. Keep/Archive/Favorite/Ready require an explicit App confirmation, and Archive means excluded from default MemoLens discovery—not deletion.
- Never use a failed, partial, running, or merely highest-numbered video analysis revision. Only the explicit successful analysis head is valid.
- Never replace an unavailable `asset_source_id` implicitly. Source changes require an explicit typed `relink_source` operation and a new in-memory revision for desktop review.
- Never scan paths, walk parent directories, or return arbitrary absolute paths from mixed-media results.
- Never delete, move, rename, overwrite, retouch, save, render, export, or otherwise modify media or timeline state.
- If an action is unavailable, hand it back to the MemoLens desktop UI instead of simulating success.
