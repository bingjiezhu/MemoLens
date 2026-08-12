# Changelog

All notable changes to MemoLens are documented here. The project follows
[Semantic Versioning](https://semver.org/).

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

[0.2.0]: https://github.com/bingjiezhu/MemoLens/releases/tag/v0.2.0
