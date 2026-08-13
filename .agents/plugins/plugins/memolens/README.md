# MemoLens for Codex 0.5.0

> Your private media memory, ready when the story arrives.

Describe what you want to publish. MemoLens reads only the creator preferences you confirmed, finds the right photo or exact beat inside a video, keeps every choice tied to its source, and shapes the selection into an **unsaved** hard-cut timeline draft for desktop review.

**Orient → Remember → Find or Review → Shape → Confirm**

Nothing is silently changed. Safe-default access stays local and read-only, uses no third-party model API key, and exposes no save, render, export, delete, or arbitrary-path write tool.

## Start with an idea

Try asking Codex:

- “Find the strongest photo and video moments for a quiet one-minute travel story.”
- “Use my confirmed creator style and show which preferences you applied.”
- “Show the unreviewed photo and video inbox, then suggest what fits this idea.”
- “Shape these selected moments into an unsaved hard-cut timeline.”

The natural flow is intentionally short:

1. **Check** which local indexes are ready.
2. **Remember** the latest confirmed Creator Memory profile; no raw prompt or hidden inference is returned.
3. **Find or review** a small source-grounded shortlist, or browse Inbox state without changing it.
4. **Shape** selected moments into an in-memory Timeline 1.0 draft and validate it.
5. **Confirm in the App.** Inbox decisions, profile edits, imports, renders, and exports remain App-owned actions.

## What stays private

Safe-default commands perform no socket or DNS call. SQLite must already be in WAL mode. MemoLens copies a stable DB/WAL snapshot into a private temporary directory, validates the complete copied WAL and its checksums, then queries only that snapshot with `mode=ro`, `query_only=ON`, foreign keys enabled, and a 5-second busy timeout. It never opens or creates the original SHM and leaves the original DB, WAL, SHM, and directory entries byte-for-byte untouched; unsafe, changing, or rollback-journal databases fail closed. Video search uses only the explicit current successful analysis head—never an inferred highest revision.

Mixed photo and video matches both carry stable `asset_id`, `asset_source_id`, and SHA-256 provenance, so either can enter Timeline drafting without path guessing. Archived assets are omitted from default photo/video retrieval and mixed-media browsing, while explicit media detail and the Archived Inbox remain available and clearly mark the current review state. Mixed and Inbox output never includes an absolute path.

Creator Memory is **confirmed-only**. The plugin returns a bounded profile projection, revision, content hash, and evidence counts. It strips unknown fields and never returns raw chats, raw prompts, or provider payloads. `memolens_inbox_list` is equally read-only: Codex may explain or suggest Keep/Archive/Favorite/Ready, but the user must confirm the final diff in MemoLens. Archive changes MemoLens discovery metadata only; it never deletes or moves the original file.

An optional, user-controlled `MEMOLENS_PLUGIN_TRUST_LOCAL_API=1` setting unlocks only additional loopback **read** views such as memory clusters. It never grants write, render, export, indexing, or cancellation capability.

## Developer entry points

Configure `MEMOLENS_DB_PATH` when fixed application-state discovery is not available, then use the MCP server in `.mcp.json` or the standard-library CLI:

```bash
python3 scripts/memolens_cli.py status
python3 scripts/memolens_cli.py creator-context
python3 scripts/memolens_cli.py inbox-list --state inbox --kind image --kind video
python3 scripts/memolens_cli.py mixed-search "海边日落"
python3 scripts/memolens_cli.py video-search "海边日落"
python3 scripts/memolens_cli.py timeline-validate --input timeline.json
```

All CLI output is one JSON value. The scripts work from a non-repository current working directory and require only the Python standard library.
