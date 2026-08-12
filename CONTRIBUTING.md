# Contributing to MemoLens

Thanks for helping make private photo libraries easier to explore without turning them into a cloud dependency.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
npm ci
npm run dev:local
```

The backend binds to loopback by default. Use a disposable photo folder and database while developing; never commit real photos, SQLite files, `.env` files, generated exports, or Application Support data.

## Before opening a pull request

```bash
npm run check
cd photon-bot && npm ci && npm audit --audit-level=high && npm test && npm run build
```

Keep changes scoped, explain the user journey they improve, and add a regression check for data-loss, privacy, or indexing fixes. UI changes should be checked at both a desktop viewport and 390×844 without horizontal overflow.

## Architecture rules

- Flask-owned retrieval code belongs in `backend/src/retrieval/`.
- Shared SQLite, configuration, and Atlas logic belongs in `core/`.
- The renderer must access local files through validated backend preview endpoints.
- Local-only endpoints must remain loopback-only and must not trust arbitrary origins or services merely because they use a local port.
- New model integrations must preserve a no-key, local fallback path.

By contributing, you agree that your contribution is licensed under the MIT License.
