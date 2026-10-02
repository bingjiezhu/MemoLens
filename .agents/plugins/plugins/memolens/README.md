# MemoLens Agent Plugins 0.10.1

> Your private media memory, ready when the story arrives.

Describe what you want to publish or resume an existing project. MemoLens reads only the creator preferences you confirmed, lets an Agent navigate a live map of indexed media down to exact video spans, restores the canonical persisted Creative Blueprint proposal, and opens the same project-bound canonical Timeline editor from Codex or DeepSeek Harness. The editor keeps current head N separate from a read-only historical revision K. Its graphical Timeline includes a ruler, scrub playhead, zoom, verified same-session clip thumbnails, drag reorder, trim handles, video **Split at playhead**, and **Remove clip from Timeline**. The original four edits use `timeline.apply_edit`; Split/Remove use independent `timeline.apply_structural_edit` authority and a top-level `structural_edit`. Remove changes only the Timeline occurrence and leaves original media unchanged. The page can also stage K for append-only restore as N+1. With separately approved `timeline.preview_media`, supported MP4/H.264 clips can play through a verified source-preview proxy. Raw MP4 transport may contain audio, while the page disables audio playback and keeps output muted. Audio-stream presence, content, and mix are not attested; playback is not transition, render, export, or final-fidelity proof. Save succeeds only after exact paired authority, CAS, and canonical reread. A separate **Unsaved Draft Lab** keeps its broader Timeline 1.0 experiments process-scoped and **Not saved**.

**Orient → Resume or Remember → Browse, Find, or Review → Shape → Confirm**

An unconfigured host has one deliberately smaller exception: `memolens_library_bootstrap_start(request_idempotency_key)` writes only a bounded, 0600, path-free intent into a private 0700 app-state spool; `memolens_library_bootstrap_status(request_id)` reads only its path-free state. The request never carries a path, DB locator, project seed, token, proof, or raw idempotency key and never contacts the backend. `queued_open_memolens` means the user must open MemoLens themselves; there is no claimed packaged-app wake. Electron may then show its own native directory chooser before backend or renderer startup. Cancellation writes no desktop settings, DB, or project. `library_authority_staged` is a recoverable desktop projection state. `library_authority_committed` means Core durably created the exact Library, database, project, unverified Blueprint, and immutable receipt-bound scan job. It still does not mean scanning started or completed, the Blueprint was confirmed, an editor is ready, or Timeline editing was granted. After committed, `memolens_status` exposes only a path-free `database.library_scan` projection from that exact receipt-anchored job; it never promotes a newer or rogue scan row and never returns the checkpoint, cursor, root fingerprint, or path.

Current validation of this bootstrap-to-status projection is controlled and local. It does not stand in for a fresh Codex or DeepSeek model call, a real user-Library scan-worker journey, or host/editor acceptance.

Nothing durable is silently changed. Data tools and safe-default SQLite access stay local and read-only and use no additional MemoLens model API key. `memolens_canonical_editor_handoff(project_id)` is the primary editing entry: only the Browser's explicit Save may invoke a server-held, short-lived project capability for one exact `timeline.apply_edit`, `timeline.apply_structural_edit`, or `timeline.restore_revision` operation. Those actions are independent; no action implies another. The page never receives proof material, desktop/main tokens, source paths, or a generic write tool. `memolens_editor_handoff` remains the **Unsaved Draft Lab** and never reaches the canonical ledger. Neither editor can confirm creative decisions, render, export, publish, modify original media, or choose a filesystem path.

## Install a host adapter

From the MemoLens repository root, install the Codex plugin and start a new Codex task:

```bash
codex plugin marketplace add "$(pwd)"
codex plugin add memolens@memolens-local
```

Or install the same package directory as a DeepSeek Harness bundle in its Web profile:

```bash
dsh plugin --profile web add "$(pwd)/.agents/plugins/plugins/memolens"
dsh --profile web --dump-config
dsh --profile web
```

When running DeepSeek Harness from a source checkout, use `pnpm dsh plugin --profile web add <absolute-package-path>`, `pnpm dsh --profile web --dump-config`, and `pnpm dsh --profile web` from that checkout. The DeepSeek adapter is loader-validated against the official `0.1.1-rc.2` snapshot at commit [`b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`](https://github.com/deepseek-ai/deepseek-harness/commit/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e) and the `0.1.0-rc.5` source release at commit [`47f943859bef60e4160492346772ded9b24f765a`](https://github.com/deepseek-ai/deepseek-harness/commit/47f943859bef60e4160492346772ded9b24f765a). Later developer-preview versions may break compatibility and require revalidation. See the focused [DeepSeek Harness guide](deepseek-harness/README.md).

## Start with an idea

Try asking Codex or DeepSeek Harness:

- “Find the strongest photo and video moments for a quiet one-minute travel story.”
- “Use my confirmed creator style and show which preferences you applied.”
- “Show the unreviewed photo and video inbox, then suggest what fits this idea.”
- “Show what my media Wiki currently covers, then follow the strongest result to its exact evidence.”
- “Resume my current project, show its latest observed cut, and tell me what remains unresolved.”
- “Open project proj_123 in the MemoLens Canonical Editor and let me move one clip.”
- “Open project proj_123 so I can split the selected video at the playhead or remove one occurrence without deleting the original.”
- “Inspect revision 2 without changing the current cut, then let me explicitly restore it as a new revision.”
- “Shape these selected moments into an unsaved draft in the MemoLens Unsaved Draft Lab.”

The natural flow is intentionally short:

0. **Bootstrap only when no Library authority exists.** Call `memolens_library_bootstrap_start` with a stable idempotency key, ask the user to open MemoLens when the result is `queued_open_memolens`, and poll `memolens_library_bootstrap_status` by opaque ID. Never ask the Agent for a directory path. `native_cancelled` and `expired` require a new key; keep the same request through recoverable `library_authority_staged` until `library_authority_committed`.
1. **Check and wait on exact scan truth.** Call `memolens_status`; after bootstrap commits, read only `database.library_scan`. Its closed projection reports the receipt-bound job's status/stage, progress, attempt, cancellation/resume state, processed/imported/skipped/rejected/child-job/batch counts, `no_supported_media`, fixed unknown ETA, and at most a path-free error code/retryability pair. A completed or zero-media scan does not by itself make the editor or canonical Timeline ready.
2. **Resume or remember.** Open an existing project Resume Capsule when continuing work; otherwise read the latest confirmed Creator Memory profile. Neither path imports raw chat or hidden inference.
3. **Browse, find, or review** a small source-grounded shortlist, progressively open live Wiki pages, or browse Inbox state without changing it.
4. **Read or validate the creative spine.** If a canonical Blueprint head exists, read its exact revision, decision-authority projection, and Blueprint-only operation history. Otherwise project an existing legacy brief or validate a new candidate through independent schema, base-binding, reference-resolution, and coverage-readiness axes. None of these reads proves user confirmation or writes project state.
5. **Pair the exact project for the intended operations.** Run `agent-pair`, compare the short code in MemoLens Desktop, and approve the exact project, ordered action set, expiry, and operation count. Use `timeline.apply_edit` for the four original controls, `timeline.apply_structural_edit` for video Split or Timeline-occurrence Remove, `timeline.restore_revision` for append-only history restore, and `timeline.preview_media` only for bounded verified source-preview transport. No action implies another. Pairing is scoped permission, never semantic confirmation.
6. **Edit, play supported source previews, or inspect history in the active host.** Call `memolens_canonical_editor_handoff` with only `project_id`. Codex opens the exact `browserHandoff.url` in its in-app Browser. DeepSeek calls `mcp__memolens__memolens_canonical_editor_handoff` and presents **Open MemoLens Canonical Editor**. Both require a user gesture and use the exact returned URL. The server derives and cross-checks current Blueprint/Coverage/Timeline state. The Browser exposes a ruler, scrub playhead, zoom, session-bound verified thumbnails, drag reorder, trim handles, video Split, and Remove without disclosing a source path. Split uses absolute `source_split_ms`, requires at least 100ms on each side, and refuses a result above 256 clips. Remove refuses the last visual clip and leaves original media unchanged. Both enter `Pending · not canonical`; Discard writes nothing, while Save submits top-level `structural_edit` and reports N+1 only after exact reread. When `timeline.preview_media` is approved and the current clip is supported MP4/H.264, Play/Pause/seek use a short-lived, project/head/clip-bound verified source preview. Raw MP4 transport may contain audio, while the page disables audio playback and keeps output muted. Audio-stream presence, content, and mix are not attested. Historical K remains read-only and restore pending is mutually exclusive with edit/structural pending. Playback does not imply final fidelity, render, or export.
7. **Use the Draft Lab only for exploration.** For its wider split/delete/fit/volume/canvas/undo/redo dialect or a caller-supplied Timeline 1.0 value, call `memolens_editor_handoff`. It is always **Unsaved Draft Lab / Not saved / process-scoped** and cannot enter the canonical ledger.
8. **Keep decisions App-owned.** Only native decision review can confirm or revoke semantic units. Inbox decisions, profile edits, render, export, and publish remain outside the editor. Desktop/Electron is the pairing approval/revoke and runtime boundary, not the primary Timeline editing interface.

## What stays private

Safe-default data commands perform no socket or DNS call. SQLite must already be in WAL mode. MemoLens copies a stable DB/WAL snapshot into a private temporary directory, validates the complete copied WAL and its checksums, then queries only that snapshot with `mode=ro`, `query_only=ON`, foreign keys enabled, and a 5-second busy timeout. It never opens or creates the original SHM and leaves the original DB, WAL, SHM, and directory entries byte-for-byte untouched; unsafe, changing, or rollback-journal databases fail closed. Video search uses only the explicit current successful analysis head—never an inferred highest revision. Explicit editor handoffs start one loopback-only server in the MCP process. Canonical pending state stays bounded and noncanonical until Save; the Draft Lab remains wholly transient.

Photo summaries in `memolens_search`, mixed search, and Wiki search are admitted only after the plugin re-verifies the exact-current canonical image result/head/source binding, active clean projection generation, receipt, manifest, projected/physical row, aliases, and artifacts. Legacy-only, pending, stale, tampered, or wrong-database/source rows are excluded. The photos-only tool may retain a traversal-checked local path for an admitted current result so Codex can explicitly inspect a small sample; that path is convenience only, never identity or authority. Mixed and Wiki output remain path-free. The optional loopback read opt-in cannot override this image authority.

Admitted mixed photo and video matches both carry stable `asset_id`, `asset_source_id`, and SHA-256 provenance, so either can enter Timeline drafting without path guessing. Archived assets are omitted from default photo/video retrieval and mixed-media browsing, while explicit media detail and the Archived Inbox remain available and clearly mark the current review state. Mixed and Inbox output never includes an absolute path.

The Agent Media Wiki v0 is a live, read-only projection over those same facts. It supports status, paginated Asset pages, search-to-page references, Library/Asset/current-Span pages, and exact Asset or Span evidence. Every response says `generation: null` and `cross_request_consistency: not_pinned`: this first slice is immediately useful for navigation, but it is not yet a materialized, replayable Wiki generation. File names, descriptions, OCR, subtitles, and model observations are marked as untrusted data; provider payloads, cache paths, absolute Library/DB paths, and raw media bytes are not returned.

Project Resume is an honest read projection. `project-list` finds active/draft work but reports only row presence. `project-open` verifies an explicit Creative Blueprint head when present, marks that persisted head as the technical authoritative project selection, and prioritizes its authority state, outline, and open decisions; semantic authority still remains unverified. It reports the legacy brief and latest observed Timeline as non-canonical migration context. A damaged head/revision/digest/creation-operation join fails closed and never falls back to `MAX(revision)`, an older Blueprint, or the legacy brief. `project-history` remains the bounded legacy Timeline view, while `blueprint-history` is the separate Blueprint-only operation ledger. The canonical editor's revision picker reads the canonical Timeline revision ledger only; none of these surfaces is complete cross-resource project history.

`blueprint-get` reads the explicit current head or one exact immutable revision through a single private snapshot. It verifies the frozen persisted schema, canonical JSON, document/row identity, semantic and decision-unit digests, creation operation, and the separate decision-authority ledger before returning semantic content. It reports immutable `creation_authority` separately from the revision-scoped `authority_projection`; partial or full unit confirmation never implies planning readiness, renderability, or permission to publish. `blueprint-history` returns only bounded operation and revision summaries: no script text, reference locator, actor/origin payload, idempotency key, path, or raw operation JSON. Its `coverage_scope=creative_blueprint` and `complete_project_history=false` fields are deliberate.

Creative Blueprint preflight remains the migration path. `blueprint-shadow` maps only verified legacy fields into `memolens.creative_blueprint_candidate/v1`, and `blueprint-validate` returns a canonical digest, bounded diagnostics, four independent axes, and `authority_verified: false`. A candidate is never silently promoted to persisted truth. MCP exposes no Blueprint mutation. The standard-library CLI can request one native-approved capability and then submit exact-CAS `blueprint-commit` or `blueprint-restore` commands; every resulting revision is still an Agent proposal, never synthetic user confirmation.

Pairing uses an independent proof protocol rather than the renderer token or `MEMOLENS_PLUGIN_TRUST_LOCAL_API`. The CLI creates the proof secret, stores it only in MemoLens app state with a private directory/file mode, and never accepts it through argv or prints it. The default capability lasts 15 minutes and 20 operations, is bound to one database runtime, existing project, and an ordered explicit action set. Ordinary edit, structural edit, restore, and preview are independent actions; granting one never grants another. Canonical Save obtains a one-time nonce and request-bound HMAC inside the plugin process; the Browser receives neither. Expiry, revoke, operation exhaustion, or backend restart blocks new saves. The displayed Codex/DeepSeek/vendor label is a claim, not verified identity, and Save is not semantic confirmation.

Creator Memory is **confirmed-only**. The plugin returns a bounded profile projection, revision, content hash, and evidence counts. It strips unknown fields and never returns raw chats, raw prompts, or provider payloads. `memolens_inbox_list` is equally read-only: the host Agent may explain or suggest Keep/Archive/Favorite/Ready, but the user must confirm the final diff in MemoLens. Archive changes MemoLens discovery metadata only; it never deletes or moves the original file.

An optional, user-controlled `MEMOLENS_PLUGIN_TRUST_LOCAL_API=1` setting unlocks only additional loopback **read** views such as memory clusters and cleanup review. It cannot supply or replace a photo Search result, which always comes from the exact-current canonical SQLite verifier. Representative assets and descriptions inside the older Atlas/cleanup API views remain unverified navigation/review hints and must be re-resolved through canonical Search or Wiki before being treated as image-analysis evidence. The opt-in never grants write, render, export, indexing, or cancellation capability.

## Developer entry points

Configure `MEMOLENS_DB_PATH` when fixed application-state discovery is not available, then use the MCP server in `.mcp.json` or the standard-library CLI:

Codex exposes raw MCP names such as `memolens_library_bootstrap_start`, `memolens_library_bootstrap_status`, `memolens_status`, `memolens_canonical_editor_handoff`, and `memolens_editor_handoff`. DeepSeek Harness exposes the same tools as `mcp__memolens__<raw-name>`. The canonical tool is the primary project editor; the legacy tool is the Unsaved Draft Lab.

```bash
python3 scripts/memolens_cli.py library-bootstrap-start --request-idempotency-key first-library-001
python3 scripts/memolens_cli.py library-bootstrap-status lb_<opaque-id>
python3 scripts/memolens_cli.py status
python3 scripts/memolens_cli.py creator-context
python3 scripts/memolens_cli.py inbox-list --state inbox --kind image --kind video
python3 scripts/memolens_cli.py mixed-search "海边日落"
python3 scripts/memolens_cli.py video-search "海边日落"
python3 scripts/memolens_cli.py wiki-status
python3 scripts/memolens_cli.py wiki-list --kind image --kind video
python3 scripts/memolens_cli.py wiki-search "安静的海边转场"
python3 scripts/memolens_cli.py wiki-open memolens://asset/asset_123
python3 scripts/memolens_cli.py wiki-evidence memolens://evidence/span/segment_123
python3 scripts/memolens_cli.py project-list --status current
python3 scripts/memolens_cli.py project-open proj_123
python3 scripts/memolens_cli.py project-history proj_123 --limit 50
python3 scripts/memolens_cli.py blueprint-shadow proj_123
python3 scripts/memolens_cli.py blueprint-validate --input blueprint-candidate.json
python3 scripts/memolens_cli.py blueprint-get proj_123
python3 scripts/memolens_cli.py blueprint-get proj_123 --revision 2
python3 scripts/memolens_cli.py blueprint-history proj_123 --limit 50
python3 scripts/memolens_cli.py agent-pair proj_123 --client-label "Codex" --action timeline.apply_edit --action timeline.apply_structural_edit --action timeline.restore_revision --action timeline.preview_media
python3 scripts/memolens_cli.py agent-pair-status proj_123
python3 scripts/memolens_cli.py agent-capability-status proj_123
python3 scripts/memolens_cli.py blueprint-commit proj_123 --input commit.json --idempotency-key proposal-001
python3 scripts/memolens_cli.py blueprint-restore proj_123 --input restore.json --idempotency-key restore-001
python3 scripts/memolens_cli.py timeline-validate --input timeline.json
```

All CLI output is one JSON value. Pairing and write commands need the running MemoLens backend plus native desktop review; all other safe-default commands retain their read-only SQLite behavior. The scripts work from a non-repository current working directory and require only the Python standard library.
