# MemoLens Autonomous Iterations

This file tracks the productization passes that are being applied directly in the repo.

## Current Direction

The working target is a local-first macOS photo agent with:

- desktop-managed backend startup
- backend-owned runtime settings
- app-state storage separated from original photos
- one clear retrieval runtime boundary
- visible, inspectable query/filter state in the UI
- pluggable providers including MiniMax and Vertex AI

## Completed Passes

### Pass 1

- Rewrote the root README into a real project-level document.
- Added the Figma architecture image to the docs.

### Pass 2

- Switched the default stack to MiniMax-first plus local semantic hashing.
- Fixed local folder / db overrides so the desktop app actually queries the selected library.
- Added local fallbacks when MiniMax vision is unavailable.

### Pass 3

- Added Electron-managed backend startup.
- Added persisted desktop settings.
- Added a visible Control panel in the UI.
- Added a macOS bootstrap script.

### Pass 4

- Added backend-owned persisted settings with runtime reload.
- Separated backend app state from the photo library.
- Made the backend the single SQLite schema owner.
- Added Vertex provider support for query / vision / copy flows.

### Pass 5

- Added a backend-owned retrieval import boundary under `backend/src/retrieval/`.
- Updated runtime callers and smoke-test scripts to import retrieval services from the backend-owned path.
- Surfaced parsed query / filter chips in the React UI so retrieval is easier to inspect.

## In Progress

- Replace the remaining backend-owned retrieval shims with fully relocated source files.

## Next Queue

- Replace the remaining `frontend/querying` runtime ownership with backend-owned modules.
- Add filter chips and a structured query inspector to the result view.
- Add ignore rules, sidecar metadata ingestion, and reindex/reset actions.
- Add a first-run onboarding flow for library selection and indexing.
- Add a signed macOS packaging path after the runtime boundary stabilizes.
