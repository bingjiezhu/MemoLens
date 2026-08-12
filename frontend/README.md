# Frontend Query Runtime (Python)

This folder is **not** the React UI.

The Electron / Vite renderer lives in repo-root `src/`.  
This `frontend/` package still hosts the active Python retrieval stack for historical reasons:

```text
frontend/querying/
  planner.py      # natural-language → structured query
  retrieval.py    # multi-signal ranking + diversity rerank
  copywriter.py   # grounded title / caption / highlights
```

`backend/src/retrieval/` re-exports these modules so the Flask API can keep a stable import path while the package boundary migrates.

## Responsibility Split

| Layer | Location | Owns |
| --- | --- | --- |
| Renderer UI | `src/`, `src/query/` | Compose, Workbench, Control, HTTP client, Electron bridge |
| Flask API | `backend/` | Routes, settings, indexing jobs, Atlas endpoints |
| Query engines | `frontend/querying/` | Planning, retrieval, copy generation |
| Indexing / shared | `indexing/`, `core/` | EXIF, vision, vectors, SQLite, Atlas derivation |

Default local photo placeholder: `./local-photo-library`  
Default managed SQLite (macOS desktop): `~/Library/Application Support/MemoLens/storage/photo_index.db`
