# Optional MCP plugin

This directory is **not** required to run the MemoLens desktop app.

It ships a read-only MCP plugin that can inspect an existing MemoLens SQLite library from a local conversational client. The safe default never writes Inbox decisions, never starts FFmpeg, and never talks to `localhost` unless you explicitly opt in.

Marketplace entry: `.agents/plugins/marketplace.json`  
Plugin: `.agents/plugins/plugins/memolens/`  
Install notes: `.agents/plugins/plugins/memolens/README.md`
