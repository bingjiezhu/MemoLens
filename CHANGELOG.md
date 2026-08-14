# Changelog

All notable changes to MemoLens are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Docs

- Added a 50-second English walkthrough to the README homepage: remember locally, review Inbox, find a moment, then make a 720p first cut. Captions only; instrumental score, no voiceover. The MP4 lives at `docs/assets/memolens-promo.mp4` and on the `promo` GitHub Release for inline playback.

## [0.5.0] - 2026-08-12

### Added

- A unified photo/video Media Inbox with reversible Keep, Archive, Favorite, Ready, and Undo metadata that never moves or deletes original media.
- A versioned Creator Memory profile whose platform, audience, format, tone, pace, and constraints are only applied after explicit confirmation and retain evidence-linked provenance.
- Direct Codex tools for confirmed creator context and Inbox browsing through strict read-only SQLite, without another Agent runtime or model API key.
- Spec 006, defining the App-owned state boundary, creator-facing information architecture, v3 migration, plugin contract, and non-destructive review model.

### Changed

- Reframed MemoLens as a complete private media home for short-video creators: drop material once, rediscover and review it naturally, then describe the next story in the App or Codex.
- Made archived assets opt out of default discovery and creation while remaining locally recoverable and valid for historical projects.
- Unified mixed photo/video provenance so Codex can turn either media type into an unsaved Timeline 1.0 draft.

### Security

- Kept all Inbox/profile writes behind the authenticated desktop session, idempotency keys, database binding, and optimistic revision checks; the Codex plugin remains zero-write and API-off by default.
- Preserved the published v2 migration checksum while adding schema v3 as a separate, atomic migration.
- Made import, brief, timeline, render, cancel, and resume writes freeze their exact response in the same SQLite transaction as the domain change, so a lost response cannot duplicate work.
- Allowed trusted Chromium CORS preflights to complete without weakening token or database binding on the real media mutation.
- Pinned every media request to one immutable runtime snapshot so a concurrent library switch cannot move an already-validated write into another database; retired runners now wait for in-flight requests to finish.

### Tests

- Added migration, CAS, crash-injected exact replay, CORS preflight, DB-binding, archive-filtering, profile-evidence, creator-context, Inbox pagination, zero-network plugin, responsive UI, and direct photo-to-timeline coverage.

## [0.4.0] - 2026-08-12

### Added

- A focused Home / Library / Memories / Create workspace model, responsive bottom navigation, and explicit Photo Story / Video First Cut creation modes.
- A six-step video flow — Idea, Material, Brief, Timeline, Preview, Save — that reveals one actionable panel at a time while keeping completed work reviewable.
- A new MemoLens Codex icon, concise starter prompts, and a shared Find → Select → Shape → Review plugin journey.

### Changed

- Rebuilt the visual language around a neutral system palette, restrained depth, consistent controls, 44-pixel touch targets, and progressive disclosure instead of a single long feature page.
- Moved runtime, model, embedding, and SQLite diagnostics into Advanced settings; made photo prompts the first interaction and optional inspiration secondary.
- Simplified Atlas empty/error states, mobile memory browsing, generation progress, focus treatment, and screen-reader semantics.

### Tests

- Added a pure video workflow model with locked/current/complete transition coverage, plus desktop and 390-pixel browser QA for overflow, navigation, focus, and panel disclosure.

## [0.3.1] - 2026-08-12

### Changed

- Split the largest mixed-responsibility paths into focused domain services, pure contracts, and thin facades across Flask media import/search/timeline/rendering, React draft and basket state, Electron process/index coordination, and the Codex plugin transport/read store.
- Reduced the main renderer, retrieval, timeline, desktop, frontend API, and plugin entry points while preserving their public requests, responses, error codes, ordering, timeouts, cancellation, and privacy boundaries.
- Added an enforced McCabe complexity ceiling for new or substantially changed Python code, documented architecture boundaries, and made the canonical Node test command cover every Electron and renderer model suite.

### Fixed

- Kept render source inspection compatible with supported Python runtimes by using the portable no-follow `os.stat` form.
- Made injected Electron indexing batch sizes total and safe instead of allowing a non-positive value to stall iteration.

### Tests

- Added direct characterization coverage for media imports, mixed ranking, render planning/source verification/publication, draft generation, basket persistence models, video API adapters, video sessions and job summaries, Electron backend/indexing lifecycles, and Codex plugin transport/read-only storage.

## [0.3.0] - 2026-08-12

### Added

- A local-first Video Creative Workbench spanning media memory, grounded direction, versioned timeline editing, and a playable 720p MP4 preview that Electron can save safely.
- Persistent video indexing jobs with ffprobe metadata, adaptive visual scene segmentation, representative keyframes, optional sidecar transcripts, progress, cancellation, and restart-safe states. Search in 0.3 is deterministic metadata/sidecar text rather than full semantic video understanding.
- Unified search results for existing image records and timestamped video segments, with stable provenance that can be compiled into a timeline.
- Deterministic creative briefs, storyboards, typed timeline operations, validation, immutable revisions, and bounded FFmpeg render jobs.
- A responsive Create Video experience with capability diagnostics, real loading/retry/cancel states, native media playback, and safe desktop Save As for completed previews.
- Offline Codex plugin tools for unified photo/video-segment search, timeline drafting/revision/validation, and read-only persisted-timeline inspection.
- Synthetic landscape-with-audio and silent vertical video fixtures for privacy-safe demos and real FFmpeg regression coverage.
- Spec 005, documenting the product contract, coarse-to-fine video understanding, security boundaries, schemas, UX states, and release gates.

### Changed

- Reframed MemoLens from a photo-only memory workbench into a photo-and-video creative memory system while preserving the existing image index and Atlas APIs.
- Updated macOS setup and CI to require FFmpeg 6+ and verify the media workflow on both Linux and macOS.
- Updated the architecture, product strategy, security model, privacy disclosures, and Codex onboarding for video frames, audio, transcripts, timelines, and renders.

### Security

- Kept video frame/audio analysis offline by default even when a cloud provider key is already configured; video data egress requires a separate explicit opt-in.
- Restricted expensive or state-changing media operations to authenticated desktop calls or independent scoped capabilities; originless loopback trust and the plugin's read-risk opt-in do not grant write access.
- Validated all source IDs, source time ranges, library roots, output roots, and timeline operations before invoking fixed FFmpeg argument arrays; originals are never modified.
- Made preview output and desktop Save As bounded and non-overwriting, render jobs cancellable, and Codex safe-default media tools zero-network and SQLite read-only. Final 1080p export remains fail-closed until Electron can issue a scoped output grant.

## [0.2.0] - 2026-08-12

### Added

- A guided three-step first-run journey with a single next action, recovery states, responsive layouts, and accessible progress feedback.
- A privacy-safe synthetic photo library generator for demos and end-to-end QA.
- A repository-local, read-only Codex plugin with status, search, memory, and cleanup tools plus offline SQLite fallback.
- CI, dependency auditing, regression tests, contribution guidance, a security policy, an MIT license, and product strategy documentation.
- Scoped index health and durable Atlas basket restoration.

### Changed

- Moved active retrieval ownership from the legacy `frontend/querying` package into the Flask backend.
- Upgraded the supported runtime to Python 3.10+, Node.js 22.12+, Electron 43, Vite 8, and TypeScript 7.
- Preserved Unicode metadata and queries throughout retrieval and Atlas flows.
- Made generation, Atlas loading, health checks, and basket persistence cancellable, time-bounded, and resilient to stale responses.
- Improved empty, partial, failed, offline, and mobile states across the desktop and browser experience.

### Fixed

- Prevented duplicate SQLite rows when file contents or paths collide, with an atomic uniqueness migration.
- Prevented stale cross-library Atlas and basket state from overwriting the active library.
- Prevented old, out-of-order basket saves from replacing newer user edits.
- Hardened the loopback API, backend identity checks, Electron sandbox/navigation/IPC boundaries, local file serving, and release build freshness.
- Distinguished empty, partial, and failed indexing outcomes instead of presenting them as successful work.

[0.5.0]: https://github.com/bingjiezhu/MemoLens/releases/tag/v0.5.0
[0.4.0]: https://github.com/bingjiezhu/MemoLens/releases/tag/v0.4.0
[0.3.1]: https://github.com/bingjiezhu/MemoLens/releases/tag/v0.3.1
[0.3.0]: https://github.com/bingjiezhu/MemoLens/releases/tag/v0.3.0
[0.2.0]: https://github.com/bingjiezhu/MemoLens/releases/tag/v0.2.0
