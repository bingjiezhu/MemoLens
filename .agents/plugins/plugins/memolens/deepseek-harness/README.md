# MemoLens for DeepSeek Harness

This directory documents the thin DeepSeek Harness adapter for MemoLens. The bundle mounts the same agent-neutral Python stdio MCP used by Codex, adds a DeepSeek-specific Skill and prompt section, and registers dedicated DeepSeek Web tool views for the canonical project editor and the separate Unsaved Draft Lab. It does not fork MemoLens Core or create a host-local Timeline.

## Compatibility boundary

The adapter has been loader-validated against two DeepSeek Harness developer previews: the frozen `0.1.1-rc.2` snapshot at commit [`b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`](https://github.com/deepseek-ai/deepseek-harness/commit/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e), and the `0.1.0-rc.5` source release at commit [`47f943859bef60e4160492346772ded9b24f765a`](https://github.com/deepseek-ai/deepseek-harness/commit/47f943859bef60e4160492346772ded9b24f765a). Both are developer previews and may make compatibility-breaking changes; unlisted snapshots require revalidation.

Loader and focused local validation do not prove a fresh DeepSeek model call, a real user-Library scan-worker journey, or editor readiness. Those remain separate host-acceptance gates.

Requirements:

- DeepSeek Harness with its `web` profile and `dsh plugin` bundle protocol from one of the validated snapshots above
- Node.js `^22.19.0 || >=24.0.0`
- `python3` 3.10+ available to the DeepSeek Harness process; the MemoLens MCP itself needs only the Python standard library
- An existing MemoLens SQLite index, discovered from application state or selected with the environment variables below

## Install

From the MemoLens repository root:

```bash
dsh plugin --profile web add "$(pwd)/.agents/plugins/plugins/memolens"
dsh --profile web --dump-config
dsh --profile web
```

The config dump should include a `dsh-memolens` bundle layer and the `memolens-mcp`, `memolens-skill`, and `memolens-prompt` rows. Restart an already-running DeepSeek Web process after installation. When using a DeepSeek Harness source checkout, run `pnpm dsh plugin --profile web add <absolute-package-path>`, `pnpm dsh --profile web --dump-config`, and `pnpm dsh --profile web` from that checkout.

The two MemoLens Loader rows deliberately use the bare names `dsh-memolens` and `dsh-memolens/prompt`. DeepSeek resolves those names once for the Host and again when it builds the Web client roster; replacing them with profile-relative file paths can leave the MCP mounted while silently removing the dedicated MemoLens tool card.

For source-checkout validation, make sure `node` is a standalone Node executable rather than an Electron wrapper. DeepSeek's Loader needs its supported internal-module bridge to resolve the installation closure. A wrapper that reports Node 24 but cannot expose that bridge may fail from `vendor/loader/src/config/tree.ts` with unrelated in-box packages. Use a real Node `^22.19.0 || >=24.0.0` executable (or pass the upstream-supported `--expose-internals` Node flag) before diagnosing the MemoLens bundle. On browser-automation lanes, the upstream `apps/web/tests/pin-browse-picker.overlay.yml` overlay may be used to select the browse picker without modifying Harness source.

The bundle forwards these optional variables from the DeepSeek Harness process to its MCP child:

- `MEMOLENS_DB_PATH` selects one existing MemoLens SQLite database.
- `MEMOLENS_LIBRARY_DIR` selects the library used for safe discovery when an explicit DB path is absent.
- `MEMOLENS_APP_STATE_DIR` overrides application-state discovery.
- `MEMOLENS_BASE_URL` selects the trusted loopback MemoLens API endpoint for explicitly enabled read views.
- `MEMOLENS_PLUGIN_TRUST_LOCAL_API=1` opts into the additional loopback read surface. It grants no write, render, export, or media-mutation authority.

## Tool names

DeepSeek Harness registers each MCP tool as:

```text
mcp__memolens__<raw-name>
```

Examples:

| Purpose | DeepSeek Harness tool |
| --- | --- |
| Queue native Library confirmation | `mcp__memolens__memolens_library_bootstrap_start` |
| Read path-free Library request state | `mcp__memolens__memolens_library_bootstrap_status` |
| Check local readiness | `mcp__memolens__memolens_status` |
| Read confirmed creator context | `mcp__memolens__memolens_creator_context` |
| Draft an in-memory Timeline | `mcp__memolens__memolens_timeline_draft` |
| Read an exact Timeline | `mcp__memolens__memolens_timeline_get` |
| Open the canonical project editor | `mcp__memolens__memolens_canonical_editor_handoff` |
| Open the Unsaved Draft Lab | `mcp__memolens__memolens_editor_handoff` |

The server-qualified prefix is added by the official `@deepseek-ai/dsh-mcp-client`; the raw MCP operation and its contract remain shared with Codex.

When no Library authority exists, `memolens_library_bootstrap_start` accepts only a stable bounded idempotency key and stores only a derived opaque request ID in a private app-state spool. It does not accept a directory or let DeepSeek choose one. `queued_open_memolens` requires the user to open MemoLens themselves; this adapter does not claim a stable packaged-app wake. Electron then owns any native chooser. Cancellation is `native_cancelled` with zero settings/DB/project writes. `library_authority_staged` is a recoverable desktop projection state; keep polling the same opaque request. `library_authority_committed` means Core durably created the exact Library, database, project, unverified Blueprint, and immutable receipt-bound scan job. It does not prove that scanning started or completed, semantic authority was confirmed, an editor is ready, or Timeline editing was granted. After committed, call `mcp__memolens__memolens_status` and read only `database.library_scan`. That path-free projection follows the receipt's exact job, never a newest or rogue `library_scan` row, and omits the checkpoint, cursor, root fingerprint, and path. Even `scan_complete` or `no_supported_media` is not independent editor/Timeline readiness.

## Open the canonical editor

1. Pair the existing project in MemoLens Desktop with the explicit ordered action set: `timeline.apply_edit` enables the four original edit controls, `timeline.apply_structural_edit` enables video Split and Timeline-occurrence Remove, `timeline.restore_revision` enables append-only history restore, and `timeline.preview_media` enables bounded verified source-preview transport for supported current clips. No action implies another.
2. Call `mcp__memolens__memolens_canonical_editor_handoff` with only the exact `project_id`.
3. After a successful call, click **Open MemoLens Canonical Editor** in the dedicated DeepSeek Web tool card.

The tool view opens only from that user gesture; it does not auto-open a tab. It accepts only the exact loopback canonical-editor URL returned by MemoLens. If the dedicated card is unavailable, provide a clickable Markdown link using the exact `uiHandoff.url`. Do not rewrite, guess, or replace that URL.

The plugin process—not DeepSeek—reads and cross-checks the current database, project, Blueprint, Coverage, Timeline, fixed source manifest, same-Beat verified alternatives, and exact historical revisions. The Browser provides a ruler, scrub playhead, zoom, verified same-session thumbnails, drag reorder, trim handles, video **Split at playhead**, and **Remove clip from Timeline**. Split uses absolute `source_split_ms`, requires at least 100ms on each side, and refuses a result above 256 clips. Remove refuses the last visual clip and leaves original media unchanged. Both require independent `timeline.apply_structural_edit` authority and enter `Pending · not canonical`; Discard writes nothing, while Save sends one exact top-level `structural_edit`. With `timeline.preview_media` approved, supported MP4/H.264 clips can use Play/Pause/seek through a short-lived, project/head/clip-bound verified source preview. Raw MP4 transport may contain audio, while the page disables audio playback and keeps output muted; audio-stream presence, content, and mix are not attested, and the preview is not final-fidelity proof. Its history picker displays K read-only beside current N; inspecting K never changes the head. Ordinary edit, structural edit, and restore pending state are mutually exclusive. Save uses the server-held pairing credential and reports N+1 only after exact canonical reread. A new DeepSeek or Codex chat can resume the same project from `project_id`; no chat transcript or browser memory is authoritative.

Request a Timeline-only editor capability with the standalone pairing command:

```bash
python3 .agents/plugins/plugins/memolens/scripts/memolens_cli.py agent-pair proj_123 --client-label "DeepSeek Harness" --action timeline.apply_edit --action timeline.apply_structural_edit --action timeline.restore_revision --action timeline.preview_media
```

## Open the Unsaved Draft Lab

For broader Timeline 1.0 exploration, obtain a Timeline from `mcp__memolens__memolens_timeline_draft` or `mcp__memolens__memolens_timeline_get`, pass it to `mcp__memolens__memolens_editor_handoff`, and click **Open MemoLens Unsaved Draft Lab**. Its wider split/delete, fit, volume, canvas, drag reorder, undo, and redo remain **Not saved / process-scoped** and never enter the canonical ledger; they are not the canonical Split/Remove path above.

## Safety and persistence boundary

Both pages are loopback-only, one-time-bootstrapped, session-bound, and require exact Host/Origin. The canonical Browser receives no pairing secret, capability ID, nonce authority, desktop/main token, source path, source identity, or arbitrary backend URL. Under `timeline.preview_media`, it may receive only bounded source bytes through the same-origin clip proxy; the lease and all source identity remain server-side. A pending ordinary/structural/restore preview is noncanonical. Canonical Save does not:

- confirm Creative Blueprint semantics;
- disclose source paths or fixed execution source IDs, or widen the independent playback grant;
- render, export, or publish;
- move, overwrite, or delete original media; or
- grant a generic Timeline write capability.

Pairing expiry, revoke, exhaustion, or runtime restart blocks new saves. A refreshable head, binding, or source conflict discards the old pending operation and loads the current head; idempotency conflict or unknown post-commit state retains the exact pending identity for reconciliation. It never auto-rebases. Unsaved Draft Lab expiry or MCP exit may discard the whole draft. Desktop/Electron remains pairing approval/revoke and runtime management, not the primary editing interface.

## Remove

```bash
dsh plugin --profile web remove dsh-memolens
```

Restart DeepSeek Web after removal.
