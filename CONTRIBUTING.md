# Contributing to MemoLens

Thanks for helping make private photo libraries easier to explore without turning them into a cloud dependency.

The React UI lives in repo-root `src/`. `frontend/` is legacy Python compatibility imports only — do not add UI there. Flask retrieval lives in `backend/src/retrieval/`. The optional MCP plugin lives under `.agents/` and is not required for the desktop app.

## Development setup

macOS desktop (matches the README):

```bash
cp .env.example .env   # optional
npm run setup:mac
./Launch\ MemoLens.command
```

Browser / API loop:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
npm ci
ffmpeg -version
npm run dev:local
```

The backend binds to loopback by default. Use a disposable photo folder and database while developing; never commit real photos, SQLite files, `.env` files, generated exports, or Application Support data. Prefer `npm run demo:library` over private media.

## Before opening a pull request

```bash
npm test                 # Python unit + plugin + Electron/Node tests
npm run check            # lint, tests, local verify, and production build
cd photon-bot && npm ci && npm audit --audit-level=high && npm test && npm run build
```

Keep changes scoped, explain the user journey they improve, and add a regression check for data-loss, privacy, or indexing fixes. UI changes should be checked at both a desktop viewport and 390×844 without horizontal overflow.

Video changes must also use a synthetic fixture, exercise a real ffprobe/FFmpeg
round trip, confirm source hashes are unchanged, and test cancellation plus partial
output cleanup. Never use a contributor's private media as a committed fixture.

## Architecture rules

- Flask-owned retrieval code belongs in `backend/src/retrieval/`.
- Video probing, indexing, timelines, and rendering belong in the dedicated media service; long jobs must not block a Flask request thread.
- Route handlers translate HTTP only. Media discovery, ranking, timeline editing, and rendering policy belong in focused domain services with stable facade APIs.
- React pages compose feature hooks and view models. Network transport, payload normalization, persistence queues, and session storage belong in their feature modules rather than page components.
- Electron `main` registers trusted IPC; process supervision and artifact operations belong in independently testable coordinators.
- Shared SQLite, configuration, and Atlas logic belongs in `core/`.
- The renderer must access local files through validated backend preview endpoints.
- Local-only endpoints must remain loopback-only and must not trust arbitrary origins or services merely because they use a local port.
- New model integrations must preserve a no-key, local fallback path.
- FFmpeg commands use fixed argument arrays with `shell=False`; timelines contain typed operations and stable IDs, never paths or free-form filter graphs.
- Video frames, audio, and transcripts stay local unless the user separately opts in to that exact external processing step.

## Code-quality rules

- Prefer explicit data flow and small domain objects over boolean flags shared across unrelated phases.
- Keep one authoritative implementation for retries, normalization, persistence, and lifecycle cleanup; compatibility modules should be thin facades or barrels.
- Extract a responsibility when it mixes transport, domain decisions, persistence, and presentation—not merely to reduce a file's line count.
- Preserve externally observable contracts while refactoring: response fields and ordering, error codes and pointers, timeout values, idempotency keys, and security checks all require characterization tests.
- New or substantially changed Python functions should stay at McCabe complexity 30 or below. Existing hotspots should move downward and must not grow without a documented reason.
- Avoid adding a second feature to files already above roughly 1,000 lines; first create the appropriate domain boundary and keep imports directional.
- Pure mappers, ranking rules, state reducers, and persistence models should have direct deterministic tests. Async controllers additionally need cancellation, stale-response, retry, and cleanup coverage.

By contributing, you agree that your contribution is licensed under the MIT License.
