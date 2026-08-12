# MemoLens for Codex 0.4.0

> Your private media memory, ready when the story arrives.

Describe a moment. MemoLens finds the right photo or the exact beat inside a video, keeps every choice tied to its source, and shapes the selection into an **unsaved** hard-cut timeline draft for desktop review.

**Find → Select → Shape → Review**

Nothing is silently changed. Safe-default access stays local and read-only, uses no third-party model API key, and exposes no save, render, export, delete, or arbitrary-path write tool.

## Start with an idea

Try asking Codex:

- “Find the strongest photo and video moments for a quiet one-minute travel story.”
- “Shape these selected moments into an unsaved hard-cut timeline.”
- “Show me what MemoLens can access before we begin.”

The natural flow is intentionally short:

1. **Check** which local indexes are ready.
2. **Find** a small, source-grounded set of photos or video moments.
3. **Shape** the selection into an in-memory Timeline 1.0 draft.
4. **Review** the validated JSON, then confirm or import it in the MemoLens desktop app.

## What stays private

Safe-default commands perform no socket or DNS call. SQLite opens through a `mode=ro` URI and `PRAGMA query_only=ON`. Video search uses only the explicit current successful analysis head—never an inferred highest revision. If no reliable current head exists, MemoLens returns `video_index_unavailable`.

Every non-text timeline item keeps its `asset_source_id` and SHA-256 provenance. Video clips also keep the source segment, analysis-run ID, and integer analysis revision. Drafts are JSON values held in memory; the plugin does not import or persist them.

An optional, user-controlled `MEMOLENS_PLUGIN_TRUST_LOCAL_API=1` setting unlocks only additional loopback **read** views such as memory clusters. It never grants write, render, export, indexing, or cancellation capability.

## Developer entry points

Configure `MEMOLENS_DB_PATH` when fixed application-state discovery is not available, then use the MCP server in `.mcp.json` or the standard-library CLI:

```bash
python3 scripts/memolens_cli.py status
python3 scripts/memolens_cli.py video-search "海边日落"
python3 scripts/memolens_cli.py timeline-validate --input timeline.json
```

All CLI output is one JSON value. The scripts work from a non-repository current working directory and require only the Python standard library.
