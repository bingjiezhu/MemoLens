# MemoLens

**A private media home for creators — remembered locally, ready when inspiration arrives.**

Drop photos and videos into one private library. MemoLens remembers the material, helps you review it without turning memories into a cleanup chore, and brings back the right source-grounded moments when you are ready to make a post. Organize, confirm, and preview in the desktop app. Originals never leave your machine unless you choose a cloud vision profile for photos.

<p align="center">
  <a href="https://github.com/bingjiezhu/MemoLens/releases/download/promo/memolens-promo.mp4">
    <img src="docs/assets/memolens-promo-poster.jpg" alt="Watch the 50-second MemoLens walkthrough — remember locally, review Inbox, find a moment, make a first cut" width="100%" />
  </a>
</p>

<p align="center"><sub>▶ 50-second walkthrough (English captions, instrumental score, no voiceover). <a href="https://github.com/bingjiezhu/MemoLens/releases/download/promo/memolens-promo.mp4">Play MP4</a> · <a href="docs/assets/memolens-promo.mp4">download</a></sub></p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#what-you-get">Features</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="docs/product-strategy.md">Product Strategy</a> ·
  <a href="docs/specs/006-creator-memory-media-inbox.md">Creator Memory Spec</a> ·
  <a href="docs/specs/005-video-creative-workbench.md">Video Spec</a> ·
  <a href="CHANGELOG.md">Changelog</a> ·
  <a href="#model-profiles">Models</a> ·
  <a href="#photon-bot">Photon Bot</a> ·
  <a href="#privacy">Privacy</a>
</p>

---

## Why MemoLens

People remember media by scene, mood, action, place, and intent — not by filename or folder date. MemoLens keeps the library on your machine and adds a creative memory layer on top:

| You want… | MemoLens does… |
| --- | --- |
| “I have new clips from this week—show me what is worth keeping close” | Opens a reversible photo/video Inbox without moving or deleting originals |
| “Quiet mountain shots, no people, low repetition” | Plans the query, ranks grounded local media, then diversity-reranks |
| “Make a 20-second vertical travel film, calm at first and energetic at the end” | Finds grounded photos and timestamped video segments, creates an editable storyboard, and compiles a local timeline |
| “Shorten the second clip and replace the city shot” | Previews a typed diff, applies confirmed edits to a new timeline revision, validates every source range, then renders a new preview |
| A map of what your library *is about* | Builds Memory Workbench + Keyword Galaxy from the same SQLite index |
| Captions you can trust | Grounds titles / captions / highlights in retrieved evidence |
| “Make this feel like my usual short videos” | Applies only the creator preferences you pinned or confirmed, with visible evidence |

Original files, indexes, keyframes, previews, and timelines stay local in the desktop/browser workflow. Photo indexing may send a resized working copy to an API-based vision profile after the existing disclosure. Video remains stricter: frames, audio, and transcripts stay offline even when a cloud key is configured; no video-egress authorization flow is exposed yet. Query, inspiration, directing, and timeline planning operate on indexed facts rather than silently uploading the library. Photon image replies deliberately upload selected copies to Discord; see its privacy boundary below.

---

## What You Get

- **Media Memory** — scan photos plus MP4/MOV/M4V; probe locally, detect visual scene changes, select representative frames, align optional sidecar transcripts, and index real video time ranges. Current search is deterministic metadata/sidecar text, not a claim of full semantic video understanding
- **Media Inbox** — review new photos and videos with reversible Keep, Archive, Favorite, Ready, and Undo metadata; originals never move or disappear
- **Creator Memory** — keep an editable, versioned creative profile whose platform, format, tone, pace, and constraints only change after user confirmation
- **Compose** — natural-language search with exclusions, quality-aware selection, and near-duplicate suppression
- **Workbench** — memories, Keyword Galaxy, storylines, duplicate stacks, baskets, cleanup cues, and feedback
- **Director** — turn audience, platform, duration, aspect ratio, tone, and constraints into a grounded brief and storyboard
- **Editor** — reorder, replace, trim, resize, crop, and fit clips through typed, reversible hard-cut timeline revisions
- **Local Preview + Safe Save As** — validate sources, render a bounded 720p H.264/AAC preview, inspect it, then save that verified artifact through Electron without touching originals
- **Inspire** — fresh search prompts from sanitized library summaries
- **Browser-safe previews** — JPEG preview endpoints for camera formats (including HEIC when `pillow-heif` is installed)
- **Desktop shell** — Electron manages the local Flask backend, folder picking, and indexing progress
- **Photon Bot** — Discord bridge over the same retrieval API (iMessage helpers exist as experimental / doctor tooling)

When an external vision profile is unavailable, MemoLens can fall back to metadata-derived descriptions and keep query / copy flowing through the configured text profile.

Reverse geocoding is implemented but **off by default** (`geocode.enabled: false` / `ENABLE_REVERSE_GEOCODE=false`). Enabling it sends each GPS-tagged photo's precise latitude/longitude to OpenStreetMap Nominatim, whose privacy and retention terms then apply.

<p align="center">
  <img src="docs/assets/memolens-home-v050.jpg" alt="MemoLens 0.5 home workspace with Media Inbox and Creator Memory summaries" width="100%" />
</p>

<p align="center">
  <img src="docs/assets/memolens-inbox-v050.jpg" alt="MemoLens 0.5 Media Inbox with reversible photo and video review" width="72%" />
  <img src="docs/assets/memolens-mobile-v050.jpg" alt="MemoLens 0.5 responsive mobile home workspace" width="22%" />
</p>

<p align="center">
  <img src="docs/assets/memolens-create-v050.jpg" alt="MemoLens 0.5 photo creation workspace with visible Creator Memory controls" width="100%" />
</p>

<p align="center">
  <img src="docs/assets/memolens-video-v050.jpg" alt="MemoLens 0.5 six-step grounded Video First Cut workspace" width="100%" />
</p>

---

## Quick Start

### Requirements

- macOS recommended for the desktop app
- Python 3.10+ (3.11 recommended)
- Node.js 22.12+
- FFmpeg/ffprobe 6+ (`npm run setup:mac` installs FFmpeg through Homebrew when needed)
- A local photo/video folder
- API keys or a local Ollama install are optional; metadata + semantic-hash fallbacks work without either

### Fastest path (macOS)

```bash
git clone https://github.com/bingjiezhu/MemoLens.git
cd MemoLens
cp .env.example .env   # optional: add a provider key or switch to Ollama
npm run setup:mac
./Launch\ MemoLens.command
```

Or build and launch from Terminal:

```bash
npm run electron
```

### First run inside the app

1. Open **Library**, choose the folder where you already save creator material, and build its private local **photo** index.
2. Index videos from **Create → Video first cut** so they join the same library. Then review new photos and videos in **Inbox**: Keep, Archive from MemoLens, Favorite, or mark Ready. Every action is reversible and leaves the file untouched.
3. Confirm the small **Creator Memory** profile you want MemoLens to reuse; unconfirmed observations never become defaults.
4. Open **Memories** to rediscover themes, or **Create** to describe the next photo story or video first cut.

Managed desktop state lives under:

```text
~/Library/Application Support/MemoLens
```

### Manual / browser fallback

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

npm install
python3 backend/app.py          # http://127.0.0.1:5519
npm run dev                     # http://127.0.0.1:5173
```

In **Control**, set backend photo library + SQLite paths, then index from **Library**. The browser path skips the native folder picker but still uses the same API.

Optional one-shot local stack:

```bash
npm run dev:local
```

Want to evaluate the full flow without pointing MemoLens at private media? Create a deterministic synthetic library first. It contains 12 generated photos plus one landscape clip with audio and one silent vertical clip:

```bash
npm run demo:library
```

Then choose `./demo-photo-library` in the app. The folder is gitignored and safe to regenerate with `npm run demo:library -- --force`.

Verify before you ship changes:

```bash
npm run verify:local
```

---

## Model Profiles

`config.yaml` ships separate **vision** and **query/copy** profiles. Defaults:

| Role | Default profile |
| --- | --- |
| Vision (indexing / grounding) | `minimax_vl01` |
| Query + copy | `minimax_m27` |
| Embeddings | `semantic_hash` (no local torch required) |

Supported families: MiniMax, Vertex / Gemini, OpenAI-compatible, DashScope, Ollama (including Gemma 4).

```bash
# MiniMax (config defaults)
export MINIMAX_KEY=...

# Vertex / Gemini
export VISION_VLM_PROFILE=vertex_gemini25_flash
export QUERY_VLM_PROFILE=vertex_gemini25_flash
export VERTEX_PROJECT="your-gcp-project"
export VERTEX_LOCATION="us-central1"

# Local Ollama
export VISION_VLM_PROFILE=ollama_gemma4_e4b
export QUERY_VLM_PROFILE=ollama_gemma4_e4b
```

When `VERTEX_ACCESS_TOKEN` is unset, the backend tries `gcloud auth application-default print-access-token`, then `gcloud auth print-access-token`.

Optional heavy local CLIP / DINO backends:

```bash
pip install -r requirements-local-models.txt
```

Point the library with env vars if you are not using the desktop picker:

```bash
export IMAGE_LIBRARY_DIR="/absolute/path/to/your/photos"
export SQLITE_DB_PATH="$IMAGE_LIBRARY_DIR/photo_index.db"
```

---

## Architecture

<p align="center">
  <img src="docs/assets/memolens-workspaces.png" alt="MemoLens 0.5 workspaces — Home, Library, Memories, and Create" width="100%" />
</p>

<p align="center"><sub>Four app rooms: Home, Library (Inbox + Creator Memory), Memories, and Create.</sub></p>

<p align="center">
  <img src="docs/assets/memolens-architecture.png" alt="MemoLens architecture — local-first layers from user surfaces to SQLite" width="100%" />
</p>

<p align="center"><sub>Technical artboard: <code>docs/assets/memolens-architecture.html</code></sub></p>

Five boundaries, one local loop:

| Layer | Code | Responsibility |
| --- | --- | --- |
| User surfaces | `src/App.tsx`, `src/library/`, `src/creator/`, `src/AtlasView.tsx`, `src/VideoWorkbench.tsx` | Home, Library, Memories, Create |
| Desktop runtime | `electron/` | Folder/preview-save pickers, hashed SQLite under Application Support, verified Flask, authenticated IPC |
| API services | `backend/src/api/routes.py` | Health, settings, photo index, inbox, creator profile, mixed search, creative/render routes |
| Intelligence | `indexing/`, `backend/src/retrieval/`, `backend/src/media/`, `core/photo_atlas.py` | Photo vision + vectors, local video probe, retrieval, Atlas, director/timeline |
| Data + policy | `core/db.py`, `core/media_db.py`, `core/config.py` | Compatible image index, media schema v3, originals untouched, loopback + desktop token |

```text
Remember  →  Review  →  Find  →  Direct  →  Edit  →  Preview
                              ↑
             authenticated Flask :5519 (loopback)
                              ↑
                    Electron  ·  Browser
```

Library **photo** indexing is `POST /v1/indexing/jobs`. Video files (MP4/MOV/M4V) enter through **Create → Video first cut** (`POST /v1/assets/import`) and then appear in the same Inbox. The Flask-owned query engines live under `backend/src/retrieval/`. The React UI lives in repo-root `src/`; `frontend/querying/` now contains compatibility imports only.

### Repository map

```text
backend/       Flask entry + HTTP API
core/          Config, SQLite, Atlas, embeddings, LLM helpers
electron/      Desktop main / preload / backend manager
frontend/      Legacy Python compatibility imports
indexing/      Photo scan, EXIF, vision, geocode, vectors
photon-bot/    Discord messaging bridge
scripts/       Bootstrap, verify, backfill, smoke tests
src/           Vite + React renderer
docs/          Specs, strategy, and homepage assets (including the 50s walkthrough MP4)
config.yaml    Library defaults + model profiles
```

Suggested reading order: `docs/specs/006-creator-memory-media-inbox.md` → `docs/specs/005-video-creative-workbench.md` → `backend/src/api/routes.py` → `backend/src/media/` → `core/media_db.py` → `src/library/` → `electron/main.ts`.

---

## API Surface

Default bind: `http://127.0.0.1:5519` (loopback-only). Override with `MEMOLENS_BACKEND_HOST` / `MEMOLENS_BACKEND_PORT` only in trusted environments.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Minimal service identity, liveness, and desktop challenge proof |
| `GET` | `/v1/index/status` | Index health scoped to one canonical SQLite path |
| `GET` / `PUT` | `/v1/settings` | Persist library paths + active profiles |
| `POST` | `/v1/indexing/jobs` | Index / rebuild a local folder |
| `POST` | `/v1/retrieval/query` | Natural-language retrieval |
| `POST` | `/v1/retrieval/copy` | Grounded title / caption / highlights |
| `POST` | `/v1/inspiration/generate` | Inspiration prompts |
| `GET` / `POST` | `/v1/atlas/*` | Status, rebuild, workbench, search, baskets, feedback, generate… |
| `GET` | `/v1/library/files/<path>` | Serve local originals (local clients) |
| `GET` | `/v1/library/previews/<path>` | Browser-safe JPEG previews |
| `GET` | `/v1/media/capabilities` | Local FFmpeg/ffprobe and video-analysis readiness |
| `POST` | `/v1/assets/import` | Safely discover a bounded media kind set and create persistent video-analysis jobs |
| `GET` | `/v1/index/jobs/<job_id>` | Persistent video-analysis job status |
| `POST` | `/v1/index/jobs/<job_id>/cancel` / `resume` | Authenticated cancellation and recovery |
| `POST` | `/v1/search/mixed` | Unified image assets + timestamped video segments |
| `GET` / `PUT` | `/v1/inbox/*` | Read or confirm reversible per-asset Inbox metadata |
| `GET` / `PUT` | `/v1/creator/profile*` | Read, suggest, or confirm the versioned creator profile |
| `POST` / `GET` | `/v1/creative/*` | Grounded briefs and creative projects |
| `GET` / `POST` | `/v1/timelines/*` | Immutable timeline revisions, typed edits, validation |
| `POST` | `/v1/renders` | Start an authenticated, hash-bound local preview job |
| `GET` / `POST` | `/v1/renders/<job_id>/*` | Preview status, cancellation, and verified Range download |

The API rejects non-loopback callers and untrusted browser origins. The Electron renderer additionally uses a per-launch authenticated session. Existing originless read clients such as curl and Photon remain part of the trusted-machine boundary, but originless loopback access does not authorize any media write or FFmpeg route. Final 1080p export remains fail-closed until a one-time Electron output-grant flow is implemented; the current app can save the verified 720p preview through the native Save As dialog.

---

## Photon Bot

Discord bridge that calls the same Flask retrieval API and replies with text + images. It fails closed unless at least one trusted Discord user ID is configured; server messages require both the user and channel allowlists. Image replies upload selected copies to Discord and may upload the original file if local resizing is unavailable and the file fits Discord's limit.

```bash
cd photon-bot
cp .env.example .env   # BACKEND_BASE_URL=http://127.0.0.1:5519
npm install
npm run doctor:discord
npm run dev
```

See [photon-bot/README.md](photon-bot/README.md). iMessage support is experimental (doctor / adapter present; Discord is the supported runtime entry).

---

## Quality Scores & Backfill

New indexes store an offline `aesthetic_score`. Upgrade an existing DB without re-running vision:

```bash
npm run quality:backfill -- --force
```

Import external LAION / NIMA-style scores:

```bash
npm run quality:backfill -- \
  --scorer external-json \
  --scores-json scores.json \
  --model-label laion-aesthetic
```

---

## Privacy

- MemoLens-managed/default photo directories, SQLite DBs, `.env`, caches, and exports are gitignored; keep arbitrary private libraries outside the repository
- Default `config.yaml` uses `./local-photo-library` as a placeholder only
- API vision profiles receive a resized working copy of each photo being indexed; choose a local Ollama profile or the metadata fallback when images must not leave the device
- Video probing, scene scanning, keyframes, sidecar transcripts, timelines, and rendering stay local; an existing cloud key never authorizes video frames or audio to leave the device
- Enabling reverse geocoding sends precise EXIF coordinates to OpenStreetMap Nominatim; it remains off by default
- Inspiration / copy paths send structured summaries and selected facts — not raw libraries or absolute private paths
- Photon image replies leave the device for Discord; use strict user/channel allowlists and trusted destinations only
- Inbox and Creator Memory are versioned metadata in the active media database. Archive never deletes or moves a source; profile suggestions do not become defaults until the user confirms them in the App
- The desktop API binds to loopback and uses a per-launch authenticated session; do not expose port `5519` through a public tunnel
- Prefer Application Support (desktop) over writing state into the photo folder itself
- Preview files are new artifacts created in app-managed storage; Electron Save As downloads through a bounded temporary file and publishes without overwriting an existing destination; source media is never overwritten

---

## Development Status

Public beta with a local-first media memory and creative loop:

- Electron can manage Flask and desktop settings
- Indexing writes metadata, semantics, embeddings, and quality into SQLite
- Retrieval supports planning, negatives, semantic scoring, quality, and duplicate suppression
- Workbench derives Atlas assets from the same index
- Query planning, ranking, and copy generation are owned by `backend/src/retrieval/`
- Video jobs produce timestamped scene segments and representative keyframes without requiring a cloud model; current retrieval uses deterministic metadata and optional sidecar text while richer visual/audio semantics remain a later Spec 005 phase
- Create Video turns grounded matches into validated, versioned timelines and local 720p MP4 previews that can be saved through the desktop dialog

---

## Contributing & License

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) and run `npm run check` before opening a pull request. Please report sensitive issues through the process in [SECURITY.md](SECURITY.md).

MemoLens is released under the [MIT License](LICENSE). FFmpeg is an external runtime; see [third-party runtime notices](THIRD_PARTY_NOTICES.md).
