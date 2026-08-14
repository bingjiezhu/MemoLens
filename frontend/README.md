# Legacy Python Query Imports

This folder is **not** the React UI.

The Electron / Vite renderer lives in repo-root `src/`.  
The active Python retrieval stack now lives under `backend/src/retrieval/`:

```text
backend/src/retrieval/
  planner.py      # natural-language → structured query
  retrieval.py    # multi-signal ranking + diversity rerank
  copywriter.py   # grounded title / caption / highlights
```

`frontend/querying/` contains compatibility imports only so existing scripts do not break. New code must import from `backend.src.retrieval`.

## Responsibility Split

| Layer | Location | Owns |
| --- | --- | --- |
| Renderer UI | `src/`, `src/query/` | Home, Library, Memories, Create, HTTP client, Electron bridge |
| Flask API | `backend/` | Routes, settings, indexing jobs, Atlas endpoints |
| Query engines | `backend/src/retrieval/` | Planning, retrieval, copy generation |
| Indexing / shared | `indexing/`, `core/` | EXIF, vision, vectors, SQLite, Atlas derivation |

Default local photo placeholder: `./local-photo-library`  
Default managed SQLite (macOS desktop): `~/Library/Application Support/MemoLens/storage/photo-index-<hash>.db`
