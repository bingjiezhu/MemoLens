# MemoLens

MemoLens is a local-first AI photo memory workbench for personal image libraries. It turns a folder of private photos into a searchable, explorable, and reusable memory layer: users can index a local library, search with natural language, curate high-quality result sets, generate grounded captions, and reuse the same retrieval stack from the desktop app or chat interfaces.

The project is designed around a practical product loop rather than a single model demo:

- **Index** local photos into a SQLite-backed semantic library.
- **Understand** images with pluggable vision models, EXIF/GPS metadata, quality signals, and lightweight semantic vectors.
- **Retrieve** photos from natural-language requests with query planning, multi-signal ranking, exclusion handling, and diversity reranking.
- **Organize** the library through Memory Workbench, Keyword Galaxy, storylines, baskets, duplicate stacks, and cleanup cues.
- **Generate** grounded titles, captions, highlights, and fresh search inspiration from selected or summarized local evidence.
- **Share** the same backend capability through `photon-bot/` for Discord / iMessage-style workflows.

![MemoLens architecture](docs/assets/memolens-architecture.png)

Architecture diagram source: [MemoLens Architecture - Latest 2026-05-06](https://www.figma.com/board/vP1MAiLXXP29ymSYRK3cKl/MemoLens-Architecture-with-Models?node-id=48-95)

## Product Goal

MemoLens solves a common gap in personal media management: people remember photos by scene, mood, moment, place, people, and intended use, but most local libraries are still organized by filename, folder, or date. The goal is to let users work with photos the way they actually remember them, while keeping the original library local and under their control.

Typical use cases include:

- Searching with plain language, such as "night scenes by the beach last winter" or "quiet mountain photos without people"
- Indexing a local image folder into a searchable SQLite database
- Finding strong, diverse results instead of a page full of near-duplicates
- Exploring a large library as memories, concepts, places, stories, and cleanup opportunities
- Applying diversity reranking so one result set is not dominated by near-duplicate images
- Generating titles, captions, and highlights grounded in retrieved images
- Reusing the same retrieval stack from both the desktop app and chat-based interfaces

## What Works Today

- Local photo indexing: scan image folders and extract file metadata, dimensions, EXIF timestamps, and GPS data
- Image understanding: call the configured vision profile to generate `description`, `tags`, and a conservative `location_hint`
- Geo enrichment: reverse geocode coordinates into `place_name` and `country`
- Semantic indexing: generate lightweight semantic vectors and store them in SQLite without requiring local `torch/transformers` installs
- Natural-language retrieval: rewrite user prompts into structured queries, then rank with time, location, tag, and text similarity signals
- Quality-aware diversity reranking: suppress near-duplicates, prefer stronger images inside similar groups, and keep result sets visually varied
- Memory Workbench: build a local SQLite-backed semantic photo layer with memories, lenses, cleanup queues, duplicate stacks, curated baskets, feedback, and Atlas-driven generation
- Keyword Galaxy: show the local library as a photo-first semantic map, linking recurring concepts to representative memories and thumbnails
- AI Inspire: generate fresh search prompts from sanitized library summaries, then feed those prompts back into Compose and Workbench preview
- Browser-safe previews: serve local images through JPEG preview endpoints so Electron can display camera formats more reliably
- Copy generation: send retrieved images into a follow-up copywriting stage to produce title, body text, and highlights
- Local model guidance: detect local Ollama/Gemma options and suggest task-specific model profiles from the desktop control panel
- Multi-entry support: the same backend currently powers both the desktop app and `photon-bot`

When an external vision profile is unavailable, MemoLens can fall back to local metadata-derived descriptions and keep the rest of the query and copy workflow available through the configured text profile.

## Demo Flow

A complete local demo can show the full product loop:

1. Open the Electron desktop app and confirm the local backend in `Control`.
2. Choose a local photo folder and build or refresh the SQLite index.
3. Search from `Compose` with a request such as "9 mountain photos, no people, low repetition."
4. Review reranked results, browser-safe previews, matched evidence, and generated copy.
5. Open `Atlas` to rebuild Memory Workbench and inspect memories, Keyword Galaxy, storylines, duplicate stacks, cleanup cues, and basket selections.
6. Use `AI Inspire` to generate new search ideas from sanitized library summaries or selected context.
7. Run `photon-bot/` and search from Discord using the same retrieval backend.

This loop is intentionally local-first: original photos and generated SQLite indexes are not committed to the repository, and model calls receive selected or summarized context rather than full local library dumps.

## Architecture Overview

The current implementation is organized around five boundaries:

| Layer | Current code | Responsibility |
| --- | --- | --- |
| User surfaces | `src/App.tsx`, `src/AtlasView.tsx`, `photon-bot/` | Desktop workflow, Memory Workbench, Compose, chat follow-ups, and browser fallback |
| Desktop runtime | `electron/main.ts`, `electron/preload.ts`, `electron/backendManager.ts`, `electron/desktopSettings.ts` | Native folder selection, managed Flask startup, local indexing progress, pause/resume control, and safe local preview access |
| API services | `backend/src/api/routes.py` | Health/settings, indexing, retrieval, copywriting, AI inspiration, Atlas workbench, cleanup, feedback, baskets, and browser-safe JPEG previews |
| AI and memory engines | `indexing/`, `frontend/querying/`, `core/photo_atlas.py`, `core/text_embeddings.py` | Vision tagging, EXIF/GPS processing, semantic vectors, query planning, multi-signal ranking, diversity rerank, grounded copy, PCA/KMeans Atlas layers, memory groups, duplicate stacks, and storylines |
| Local data and model layer | `core/db.py`, `core/config.py`, `core/local_model_runtime.py`, `config.yaml` | SQLite `image_index` and `atlas_*` tables, persisted settings, local-first guardrails, model profile routing, and Ollama/Gemma recommendations |

The main product loop now has five connected paths:

1. **Indexing path**: Electron or the browser-backed API sends a local folder or file batch to `/v1/indexing/jobs`. The Python pipeline extracts file metadata, EXIF timestamps, GPS coordinates, optional reverse geocoding, VLM descriptions and tags, semantic vectors, text embeddings, and quality scores, then writes the result into SQLite.
2. **Retrieval path**: Compose or `photon-bot` sends a natural-language goal to `/v1/retrieval/query`. The planner rewrites the prompt into a structured query, retrieval scores candidates with text, tag, time, location, semantic, quality, and exclusion signals, then reranks for diversity before the copywriter creates a grounded title, caption, and highlights.
3. **Atlas path**: `/v1/atlas/rebuild` derives a local memory layer from the same SQLite records. It projects vectors into layout coordinates, clusters concepts, creates memory cards, detects duplicate/similar stacks, assigns roles such as cover/detail/ending, and builds cleanup queues.
4. **Workbench path**: `src/AtlasView.tsx` renders those derived records as lenses, Memory Workbench cards, Keyword Galaxy, storylines, basket selections, feedback actions, and cleanup decisions.
5. **Inspiration path**: `/v1/inspiration/generate` uses sanitized library summaries and optional basket context to propose new search prompts without sending raw file paths, database paths, API settings, or full-library text.

The current `config.yaml` keeps separate profiles for vision and query/copy work. The desktop control panel can stage local or API profiles independently, while `core/local_model_runtime.py` inspects the machine and recommends practical Ollama/Gemma profiles when they exist.

Included profile families include:

- MiniMax API profiles
- Vertex AI / Gemini profiles
- OpenAI-compatible API profiles
- DashScope profiles
- Ollama local profiles, including Gemma 4 options
- `semantic_hash` as the default dependency-light semantic vector backend

## Repository Structure

```text
backend/         Flask application entry and HTTP API
core/            Config, schemas, SQLite access, and shared text utilities
electron/        Electron main/preload layer and local indexing bridge
frontend/        Frontend-side query prototype and supplementary notes
indexing/        EXIF, image preprocessing, vision, embeddings, and geocoding pipeline
photon-bot/      Discord / iMessage integration layer
scripts/         Smoke tests and helper scripts
src/             Vite + React renderer and desktop UI
config.yaml      Library path, model profiles, and retrieval config
requirements.txt Python dependencies
package.json     Desktop frontend and Electron dependencies
```

If you want a practical reading order, start with:

- `backend/src/api/routes.py`
- `backend/src/__init__.py`
- `indexing/pipeline.py`
- `frontend/querying/retrieval.py`
- `frontend/querying/copywriter.py`
- `core/photo_atlas.py`
- `src/App.tsx`
- `src/AtlasView.tsx`
- `src/query/api.ts`
- `electron/main.ts`
- `photon-bot/src/agent.ts`

## Quick Start

### 1. Prepare the Environment

Recommended environment:

- Python 3.9+
- Node.js 18+
- A local image folder that the app can access
- The model service or API keys required by the profiles in `config.yaml`

The current `config.yaml` defaults the image folder to `./local-photo-library`. For a real library, either point it at your own folder through environment variables or pick a folder in the Electron app:

```bash
export IMAGE_LIBRARY_DIR="/absolute/path/to/your/photos"
export SQLITE_DB_PATH="$IMAGE_LIBRARY_DIR/photo_index.db"
```

If you use the current MiniMax API profiles, set:

```dotenv
MINIMAX_KEY=
```

For a local model route through Ollama, select one of the `ollama_gemma4_*` profiles in the desktop control panel or set:

```bash
export QUERY_VLM_PROFILE=ollama_gemma4_e4b
```

If you want to use Vertex AI on a Mac that already has `gcloud` authorization, MemoLens now supports Vertex provider profiles as well. The practical setup is:

```bash
export VISION_VLM_PROFILE=vertex_gemini25_flash
export QUERY_VLM_PROFILE=vertex_gemini25_flash
export VERTEX_PROJECT="your-gcp-project"
export VERTEX_LOCATION="us-central1"
```

When `VERTEX_ACCESS_TOKEN` is not set, the backend will try `gcloud auth application-default print-access-token` and then `gcloud auth print-access-token`.

If you want the desktop app to start in Vertex mode by default, put the same values in the repo-local `.env` file. `VERTEX_PROJECT` is optional when `gcloud config get-value project` already returns the project you want to use.

If you switch to another provider, supply the corresponding environment variables defined by the profile in `config.yaml`, such as `OPENAI_API_KEY`, `DASHSCOPE_API_KEY`, or any compatible service settings.

### 2. Install Python Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On macOS, you can also bootstrap the desktop app in one step:

```bash
npm run setup:mac
```

Or launch it directly from Finder / Terminal with:

```bash
./Launch\ MemoLens.command
```

You can also run a local deployment verification pass before opening the UI:

```bash
npm run verify:local
```

If you intentionally want to go back to legacy local CLIP / DINO backends, install the optional model dependencies too:

```bash
pip install -r requirements-local-models.txt
```

### 3. Install Desktop Dependencies

```bash
npm install
```

If you also want the chat integration, install dependencies there too:

```bash
cd photon-bot
npm install
cd ..
```

### 4. Start the Desktop App

```bash
npm run electron
```

The Electron shell now tries to auto-start the local Flask backend by using the Python interpreter saved in the desktop settings. The first thing you should do inside the app is open the `Control` section and confirm:

- backend URL
- Python command
- desktop default photo library
- desktop default SQLite path
- backend-managed photo library
- backend-managed SQLite path
- auto-start behavior

By default, managed app state now lives under:

```text
~/Library/Application Support/MemoLens
```

If you still want to run the backend manually, the command remains:

```bash
python3 backend/app.py
```

The backend listens on `http://127.0.0.1:5519` by default and exposes these core endpoints:

- `GET /healthz`
- `POST /v1/indexing/jobs`
- `POST /v1/retrieval/query`
- `POST /v1/retrieval/copy`
- `POST /v1/inspiration/generate`
- `GET /v1/atlas/status`
- `POST /v1/atlas/rebuild`
- `GET /v1/atlas/workbench`
- `GET /v1/atlas/memory/<memory_id>`
- `GET /v1/atlas/cleanup`
- `GET /v1/atlas/overview`
- `POST /v1/atlas/search`
- `POST /v1/atlas/select`
- `POST /v1/atlas/query-preview`
- `POST /v1/atlas/feedback`
- `POST /v1/atlas/basket`
- `POST /v1/atlas/stack/action`
- `POST /v1/atlas/generate`
- `GET /v1/library/files/<relative_path>`
- `GET /v1/library/previews/<relative_path>` for browser-safe JPEG previews of local photos

The default bind host is loopback-only. If you intentionally need another bind address for a controlled environment, set:

```bash
export MEMOLENS_BACKEND_HOST="0.0.0.0"
```

Settings writes, local indexing, and direct library file serving are designed for trusted local use. The browser fallback is meant to come from `localhost` or the Electron shell, not from arbitrary remote origins.

## Privacy and Local Data

MemoLens is designed to keep personal photo data out of the Git repository:

- Source code, configuration templates, and helper scripts are safe to commit.
- API keys should live in `.env` or provider-specific environment variables; `.env` files are ignored.
- Local photo folders, generated SQLite databases, runtime logs, Electron caches, build output, and exported PDFs are ignored.
- The default `config.yaml` uses `./local-photo-library` only as a placeholder. Point it at your real library through the app settings or environment variables.
- AI Inspire and copy generation are designed to send structured summaries and selected candidate facts, not raw photo files, local absolute paths, SQLite paths, or full-library text.

Before publishing changes, run:

```bash
git status --short
npm run verify:local
```

The verification command exercises local settings, indexing, retrieval, Atlas rebuild/search/generation, preview rendering, CORS restrictions, and TypeScript checks.

## Quality Scoring and Backfill

Retrieval can use an offline `aesthetic_score` stored in SQLite. New indexing runs write this score automatically. Existing indexes can be upgraded without re-running vision models:

```bash
npm run quality:backfill -- --force
```

The default scorer is a local, dependency-light estimator that combines composition cues, sharpness, exposure, contrast, and resolution. If you generate LAION/NIMA-style scores outside MemoLens, import them with:

```bash
npm run quality:backfill -- \
  --scorer external-json \
  --scores-json scores.json \
  --model-label laion-aesthetic
```

Accepted JSON keys include `id`, `relative_path`, or `filename`, plus `score` or `aesthetic_score`.

If you want to work on the renderer separately:

```bash
npm run dev
ELECTRON_RENDERER_URL=http://127.0.0.1:5173 npx electron .
```

### 5. First Desktop Run

After the Electron window opens, the shortest path to a usable local workflow is:

1. Open the `Control` panel and confirm the backend is online.
2. Click `Choose folder` and select the local photo library you want to index.
3. If you want this folder to remain the default, copy the active library into the desktop or backend settings and save.
4. Click `Start indexing` and wait for the SQLite library to finish building. If MemoLens detects an older low-quality fallback index, this action will switch to `Rebuild index` automatically so the library can be refreshed with the active vision provider.
5. Open `Atlas` to build or refresh the Memory Workbench and use Keyword Galaxy to understand the main concepts, memory groups, cleanup queues, people risk, and storylines in the library.
6. Go to `Compose`, click `AI Inspire` when you want query ideas, or type a request like "9 mountain photos, no people, low repetition." MemoLens will preview local evidence, rerank for relevance/diversity/quality, and return filtered photos plus a generated title and caption.

The folder picker is Electron-only. In plain browser mode, MemoLens can still render the UI, but it cannot scan a local directory or write the SQLite index for you.

If Electron is blocked on your machine, there is now a browser-and-backend fallback:

1. Start the backend with `python3 backend/app.py`
2. Start the frontend with `npm run dev`
3. Open the Vite URL in your browser
4. In `Control`, set `Backend photo library` and `Backend SQLite path`, then save
5. In `Library`, click `Start indexing` or `Rebuild index`

That fallback skips the native folder picker, but it still lets the backend read a local folder path, build the SQLite index, run retrieval, and generate captions with Vertex AI.

## Photon Bot

`photon-bot/` is the chat integration layer for MemoLens. It currently does three things:

- listen for Discord / iMessage messages
- call the existing Flask retrieval API
- return text replies and image attachments

See:

- [photon-bot/README.md](photon-bot/README.md)

Common verification command:

```bash
cd photon-bot
npm run doctor:discord
```

## Development Status

The current version is a runnable local-first product prototype with a complete desktop and chat-assisted workflow:

- The Electron shell can manage the Flask backend and desktop runtime settings.
- The indexing pipeline writes local image metadata, semantic text, embeddings, and quality scores into SQLite.
- The retrieval service supports structured query planning, negative constraints, semantic scoring, quality-aware selection, and duplicate suppression.
- Memory Workbench derives Atlas assets, clusters, memories, roles, duplicate stacks, feedback records, baskets, and cleanup views from the same local index.
- The frontend exposes Compose, AI Inspire, Atlas, Keyword Galaxy, basket selection, and browser-safe image previews.
- Photon Bot reuses the Flask retrieval API from Discord and keeps short-lived channel sessions for follow-up requests.

Known engineering notes:

- `frontend/querying/` still contains the active Python query services for historical reasons. The boundary is stable, but a future cleanup can move those modules under a backend-owned package.
- Model behavior depends on the configured provider and local environment. The project keeps model profiles configurable so a demo can use API models, Vertex/Gemini, MiniMax, DashScope, or local Ollama/Gemma profiles.
- The repository intentionally excludes local photos, generated databases, `.env` files, runtime caches, exported PDFs, and other private artifacts.
