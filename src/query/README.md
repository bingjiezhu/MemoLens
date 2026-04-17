# Query Boundary

`src/query/` is the renderer-side boundary for the Electron app.

It owns:

- the TypeScript data shapes used by the React UI
- the desktop bridge helpers that talk to Electron preload APIs
- the HTTP client that talks to the Flask backend
- mock/demo data that keeps UI iteration possible when the backend is intentionally absent

It does not own:

- retrieval ranking
- query planning
- copy generation
- SQLite schema definition
- image indexing and metadata extraction

Those runtime services currently live in Python and are called through the backend API.

The current important files are:

- `types.ts` for renderer and desktop-bridge shapes
- `api.ts` for backend retrieval requests
- `desktop.ts` for Electron-only runtime functions
- `mockLibrary.ts` and `studio.ts` for UI-side demo and presentation logic

This README is intentionally short so it stays aligned with the actual runtime boundary.
