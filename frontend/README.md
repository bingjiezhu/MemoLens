# Frontend Notes

This folder is reserved for the Vite + React desktop UI for MemoLens.

The current local-first default is:

```text
./local-photo-library
```

and the default SQLite path is:

```text
~/Library/Application Support/MemoLens/state/storage/photo_index.db
```

For a real photo library, the preferred flow is to run the Electron app, choose a local folder, and let the backend write SQLite state into the managed app-data directory instead of mixing state files into the photo folder itself.

## Responsibility Split

- Frontend app layer: prompt authoring, result presentation, local assistant UX, and backend path overrides for the active library
- Backend Flask service: HTTP routes, indexing orchestration, retrieval orchestration, and provider-backed model calls
- Indexing/model layer: EXIF parsing, reverse geocode enrichment when enabled, MiniMax-first vision/copy calls, and lightweight semantic vector generation
- Shared support code lives in `core/`, so retrieval and indexing code can stay aligned across entry points
- The active model profiles and local library defaults live in the repo-root `config.yaml`

The repository still keeps active Python retrieval/query runtime code under `frontend/querying/`, while the renderer-side boundary continues to solidify under `src/query/`.

## Query Modules

The current app-side query boundary is:

```text
src/query/
  api.ts            # backend retrieval adapter + local library path overrides
  desktop.ts        # Electron bridge for folder picking and indexing controls
  mockLibrary.ts    # mock fallback content when the backend is unavailable
  studio.ts         # prompt analysis + UI-facing draft scaffolding
  types.ts          # typed result/state contracts for the renderer
```
