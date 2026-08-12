# Contributing to MemoLens

Thanks for helping make private photo libraries easier to explore without turning them into a cloud dependency.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
npm ci
ffmpeg -version
npm run dev:local
```

The backend binds to loopback by default. Use a disposable photo folder and database while developing; never commit real photos, SQLite files, `.env` files, generated exports, or Application Support data.

## Before opening a pull request

```bash
npm run check
cd photon-bot && npm ci && npm audit --audit-level=high && npm test && npm run build
```

Keep changes scoped, explain the user journey they improve, and add a regression check for data-loss, privacy, or indexing fixes. UI changes should be checked at both a desktop viewport and 390×844 without horizontal overflow.

Video changes must also use a synthetic fixture, exercise a real ffprobe/FFmpeg
round trip, confirm source hashes are unchanged, and test cancellation plus partial
output cleanup. Never use a contributor's private media as a committed fixture.

## Architecture rules

- Flask-owned retrieval code belongs in `backend/src/retrieval/`.
- Video probing, indexing, timelines, and rendering belong in the dedicated media service; long jobs must not block a Flask request thread.
- Shared SQLite, configuration, and Atlas logic belongs in `core/`.
- The renderer must access local files through validated backend preview endpoints.
- Local-only endpoints must remain loopback-only and must not trust arbitrary origins or services merely because they use a local port.
- New model integrations must preserve a no-key, local fallback path.
- FFmpeg commands use fixed argument arrays with `shell=False`; timelines contain typed operations and stable IDs, never paths or free-form filter graphs.
- Video frames, audio, and transcripts stay local unless the user separately opts in to that exact external processing step.

By contributing, you agree that your contribution is licensed under the MIT License.
