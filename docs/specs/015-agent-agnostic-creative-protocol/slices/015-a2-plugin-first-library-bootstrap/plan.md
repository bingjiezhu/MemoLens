# Implementation Plan: ML-015-A2

- Status: `PHASE 0-3 IMPLEMENTED / VALIDATED CONTROLLED LOCAL; PRODUCT/HOST PROMOTION PENDING`
- Principle: the Agent requests setup, Electron supplies only native filesystem
  authority, and Core remains the sole business-state writer.

## Phase 0: shared plugin request spool — implemented

1. Freeze the exact five-field request and six-field receipt schemas, the six
   public statuses, `expires_at_ms`, stable errors, TTL, replay, and conflict
   semantics. Persist no idempotency digest and mint no `rejected` receipt.
2. Use one private `0700` app-state spool with `0600` regular entries, 2,048
   byte entry bounds, raw census 128, and final protocol census 64.
3. Serialize Python writers with a POSIX directory `flock`; publish by fsynced
   temp plus hardlink no-replace, remove temp, then fsync the directory.
4. Recover only strictly named, bounded, identity-stable controlled temps.
   Preserve post-link `nlink=2` finals by verifying that temp and published
   target are the same inode before removing the temp.
5. Wire the same module into MCP and CLI for Codex and DeepSeek Harness and
   classify start/status with only their exact app-state effects.

## Phase 1: Electron broker-only native authority — implemented

1. Acquire the singleton and register the fixed second-instance listener before
   business IPC registration, backend startup, or window creation.
2. Parse request/receipt JSON with decoded-key duplicate rejection, including
   escaped aliases, and recognize both Python request temps and Electron
   receipt temps.
3. Claim one request per broker turn by hardlink no-replace, enforce TTL and
   in-process single-flight, and invoke the existing main-owned chooser.
4. On cancel, write a `native_cancelled` receipt with the original expiry and
   zero settings or domain changes.
5. On selection, redeem and revalidate the one-shot ticket, then hand the
   claimed request and held binding to the Phase-2 coordinator.
6. Before approved Core commit and active-runtime proof, keep cold broker-only
   startup at zero business IPC registrations, ordinary backend calls and renderer
   windows. The 2026-10-02 usability correction promotes only a fully committed
   result to normal companion startup, keeping the already active scan alive;
   cancellation, expiry, staged and failed results remain broker-only.
7. Fsync the desktop-settings temp, rename it, and fsync its parent directory.

## Phase 0-only crash boundary — retained negative regression

Safe recovery is implemented for controlled pre-publication and post-hardlink
temp artifacts, receipt-plus-request/claim leftovers, and final-census recovery.
It does not provide exactly-once behavior across this gap:

```text
desktop settings commit
  -> crash before terminal receipt
  -> restart re-prompts and may recommit
```

The isolated Phase-0 callback remains intentionally non-exact. The production
Phase-2 path closes the durable Core gap with a sealed binding and permanent
V20 replay; it does not claim exactly-once native prompting before sealing.

## Phase 2: exact runtime and V20 transaction — implemented

1. Persist a no-replace `request_id -> exact binding` envelope with a closed
   digest; pass only the request ID in the candidate process environment.
2. Add the immutable V20 bootstrap receipt with collision, backup, checksum,
   immutability, future-schema, and permanent replay tests.
3. Split schema migration from root adoption so the candidate database can
   reach V20 with zero root/project/job writes and no default ghost Library.
4. Add a main-only closed endpoint authenticated before body parsing; bind
   candidate and active health proofs to request, binding, database, schema,
   runtime mode, and runtime generation.
5. In one target transaction revalidate the held descriptor and exact
   canonical path/device/inode, then register root, deterministic scan job,
   project/Brief, minimal unverified Blueprint and ordinary Blueprint receipts,
   and finally the immutable bootstrap receipt.
6. Activate the exact runtime only after commit. Treat desktop/backend settings
   and spool status as repairable projections, not members of the SQLite
   transaction.
7. Reconcile response loss, projection failure, runtime activation failure,
   wrong-binding health, exact replay, conflicting replay, and directory rename
   without duplicate domain writes.

## Phase 3: durable Library scan and Agent progress — implemented

1. Add `media_jobs(kind=library_scan)` with a closed checkpoint and current
   root fingerprint.
2. Discover in stable relative-path order and commit bounded batches.
3. Reuse media import authority and submit child analysis jobs only after the
   corresponding batch commit.
4. Resume under the same job ID; reject root replacement, duplicate batches,
   and unknown job kinds.
5. Expose path-free progress, counts, explicit no-supported-media, and honest
   unknown ETA.
6. Let `memolens_status` join only through the immutable bootstrap receipt's
   unique `scan_job_id`; reject corrupt private state and ignore rogue jobs.

## Phase 4: real scan-to-grounded-editor acceptance — pending

1. Keep the already-created minimal `authority=unverified` Blueprint free of
   invented Wiki, Creator, evidence, or material bindings.
2. Run a real empty and populated Library through the worker/import/child-job
   path; focused fixtures do not substitute for this journey.
3. Produce verified evidence, zero-gap Coverage, and a canonical Timeline
   before attempting editor admission.
4. Keep editor status at `awaiting_grounded_timeline`; the controlled empty
   Library credential check currently fails closed as
   `canonical_editor_not_editable`.

## Phase 5: controlled-local closure complete; product/host promotion pending

1. Completed final Codex cache installation and byte-parity proof for
   `0.10.1+codex.20260830154527`: source/cache 75 files each, checksum dry-run
   zero drift, and source/cache validators passed.
2. Completed the official DeepSeek fresh-profile loader proof at HEAD
   `47f943859bef60e4160492346772ded9b24f765a` with Node 24.19 and pnpm 11.7.
   The fresh `web` profile loaded the exact cache and composed the
   MemoLens root/MCP/skill/prompt layers; cache DeepSeek tests passed 6/6.
3. Froze executable/plugin source before the final production oracle and
   `npm run check`. The oracle passed 149/149 with zero fail/skip; the full gate
   exited 0 with Python 1104/1104, source plugin 471/471, Node/renderer 129/129,
   verify, and build passing. Only evidence documentation followed, and final
   `git diff --check` remained green.
4. Retain direct cache-root `test_plugin.py` 15/16 as an explicit diagnostic:
   the sole error assumes a source-checkout marketplace path outside the cache.
   Do not turn validator/loader evidence into a native UI or model claim.
5. Separately run real Codex and DeepSeek model/host journeys, native macOS
   cancel/approve/restart journeys, real scan-to-grounded-editor acceptance,
   clean-machine packaging, Remote CI, and release gates.
6. Track pytest/unittest `ResourceWarning` output as non-blocking resource-
   hygiene debt; green functional gates do not prove a warning-free lifecycle.

## Rollback and non-inflation

- Removing only Phase 0-1 must leave existing Core data untouched.
- A queued request is not native confirmation; a staged desktop binding is not
  a Core Library; a focused loader/test result is not a real model journey.
- The isolated Phase 0-1 path writes no Core SQLite and no
  `backend-settings.json`; production approval proceeds through V20 Phase 2.
- V20 is the first layer allowed to claim request-bound durable Core
  resolution. It does not make scan completion, Coverage, Timeline, editor, or
  native/model/host acceptance true.
