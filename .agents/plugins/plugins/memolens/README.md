# MemoLens Codex plugin 0.3.0

MemoLens provides local, safe-default access to indexed photo, video, and audio metadata plus pure Timeline 1.0 planning. It uses only the Python standard library.

## Exposed capabilities

- Read-only SQLite status, unified photo/video-segment search, focused per-type search, mixed-media list/get, and persisted timeline list/get.
- Deterministic in-memory `timeline_draft`, hard-cut `timeline_revise_draft`, and render-compatible `timeline_validate`.
- Optional unauthenticated loopback **read** features after the user independently enables `MEMOLENS_PLUGIN_TRUST_LOCAL_API=1`.

The plugin exposes no project/timeline save, preview, render, export, cancel, delete, overwrite, or arbitrary-path write tool. The environment opt-in never grants those capabilities.

## Safe defaults

Safe-default commands perform no socket or DNS call. SQLite opens through a `mode=ro` URI and `PRAGMA query_only=ON`; schema capability probing supports the current `asset_analysis_heads`/successful `analysis_runs` contract and a compatible explicit legacy head. It never chooses video analysis with `MAX(revision)`. Missing reliable tables return `video_index_unavailable`.

Timeline drafts bind every non-text asset to `asset_source_id` and SHA-256 provenance. Video clips also bind their segment to an analysis-run ID and integer analysis revision. Drafts are JSON values only: review and import or confirm them in the MemoLens desktop application.

## Run

Configure `MEMOLENS_DB_PATH` when automatic application-state discovery is not available, then use the MCP server in `.mcp.json` or:

```bash
python3 scripts/memolens_cli.py status
python3 scripts/memolens_cli.py video-search "海边日落"
python3 scripts/memolens_cli.py timeline-validate --input timeline.json
```

All CLI output is JSON. The scripts work from a non-repository current working directory.
