# Feature Specification: Plugin-First Library Bootstrap

- Feature ID: `ML-015-A2`
- Created: 2026-08-30
- Implementation status: `IMPLEMENTED / PHASE 0-3`
- Validation status: `VALIDATED CONTROLLED LOCAL; PRODUCT/HOST PROMOTION PENDING`
- Parent: [ML-015 Agent-Agnostic Creative Protocol](../../spec.md)
- Plan: [plan.md](plan.md)
- Tasks: [tasks.md](tasks.md)
- Evidence: [implementation-evidence.md](implementation-evidence.md)

## Why this slice exists

MemoLens must be usable from the Agent the user already chose. A Codex or
DeepSeek Harness conversation can request Library setup without first learning
a filesystem path, database path, or second model account. Electron remains
necessary only for the native user gesture and held local filesystem identity.

The complete chain is deliberately staged:

```text
Codex / DeepSeek shared tool
  -> private app-state bootstrap request
  -> Electron broker-only native folder confirmation
  -> sealed request-bound path/device/inode binding
  -> exact candidate runtime and V20 migration
  -> one Core bootstrap transaction
  -> active runtime and repairable settings projection
  -> durable resumable receipt-bound Library scan
  -> path-free `memolens_status` scan projection
  -> minimal unverified Blueprint
  -> grounded Coverage / Timeline / editor acceptance  [unverified]
```

The production Phase-2 route does not stop at `library_authority_staged`.
`library_authority_staged` remains a repairable projection state; only the
permanent V20 receipt and exact replay permit
`library_authority_committed`. That committed state proves a queued scan job,
not scan start/completion, grounded Coverage, a canonical Timeline, or editor
readiness.

## Product and authority decisions

- Codex and DeepSeek Harness reuse one Agent-neutral MCP/CLI implementation.
- The Agent may request setup but cannot choose or approve a filesystem root.
- The request carries no Library path, database path, capability, token, proof,
  project payload, or Timeline payload.
- Electron main owns the native chooser and exact device/inode verification,
  but is not the primary editing UI.
- The Browser Canonical Editor remains the editing surface only after a
  separately grounded Timeline and separately paired edit capability exist.
- Directory confirmation grants no Blueprint semantic authority, provider
  egress, Timeline edit, export, deletion, or source-management capability.
- `desktop-settings.json` and `backend-settings.json` are repairable
  projections. Neither may precede the permanent Core commit or substitute for
  its immutable receipt.

## Public bootstrap contract

### Start

```text
memolens_library_bootstrap_start(request_idempotency_key)
```

The tool derives an opaque `lb_<64 hex>` request ID from the bounded
idempotency key, writes at most one private request, and returns the same closed
status projection used by the status tool. The idempotency key and a separate
digest field are not persisted in the request.

If no stable packaged-app wake route has been proven, the result is
`queued_open_memolens` with `next_action=open_memolens`. It must not claim that
a native dialog was shown. Every successful projection includes the exact
`expires_at_ms`. Before Core commit, all creation facts remain false. A
`library_authority_committed` receipt sets only the three durable creation facts
for Core Library/database/project; it still keeps scan/editor/edit readiness
false:

- `core_library_created` (true only after committed)
- `database_created` (true only after committed)
- `project_created` (true only after committed)
- `scan_started`
- `editor_ready`
- `timeline_edit_granted`
- `backend_contacted_by_plugin`

### Status

```text
memolens_library_bootstrap_status(request_id)
```

The status tool performs a bounded, path-free app-state read. Its complete
status set is:

- `queued_open_memolens`
- `awaiting_native_confirmation`
- `library_authority_staged`
- `library_authority_committed`
- `native_cancelled`
- `expired`

The last four are receipt states. There is no `rejected` receipt and no
`native_confirmation_open` public state. Malformed, conflicting, unsafe, or
missing artifacts fail through stable typed errors rather than minting a
terminal receipt. After `library_authority_committed`, the Agent switches to
`memolens_status` and reads only `database.library_scan` for scan progress.

### Closed private artifacts

A request contains exactly:

```json
{
  "schema_version": "1",
  "kind": "library_bootstrap_intent",
  "request_id": "lb_<64 hex>",
  "created_at_ms": 0,
  "expires_at_ms": 300000
}
```

The numeric values above illustrate the five-field shape; the invariant is
`expires_at_ms = created_at_ms + 300000`. There is no persisted idempotency
digest, path, ticket, or intent payload beyond the closed `kind`.

A receipt contains exactly schema version, receipt kind, request ID, terminal
state, `completed_at_ms`, and the request's original `expires_at_ms`. Copying
the request expiry makes a same-ID request/claim/receipt expiry mismatch a
closed conflict rather than something inferred from completion time.

### Filesystem and recovery contract

- the spool directory is private `0700`; protocol entries are regular `0600`
  files no larger than 2,048 bytes;
- raw directory census is capped at 128 names before classification; final
  request/claim/receipt census is capped at 64;
- Python serializes writers with a POSIX directory `flock` and fails closed
  when that writer lock is unsupported;
- publication is hardlink-based no-replace: fsync temp, link temp to final,
  unlink temp, then fsync the containing directory;
- Electron claims request ownership by hardlinking request to claim without
  replacement, then removing request and fsyncing the directory;
- Python recognizes and safely recovers its bounded
  `.lb_<64hex>.<32hex>.tmp` artifacts; Electron recognizes both that form and
  its `.lb_<64hex>.receipt.json.<uuid>.tmp` form;
- temp cleanup requires closed names, regular files, `0600`, bounded size,
  link-count rules, and stable device/inode identity. A post-link `nlink=2`
  temp is removed only when it is the same inode as its published final;
- an exact terminal receipt wins over same-ID request/claim leftovers only
  after the expiry values agree. Unknown or unsafe entries fail closed.

Both Python and Electron reject duplicate decoded JSON keys, including escaped
aliases such as `schema_version` plus `schema\u005fversion`. argv, deep links,
and second-instance payloads are wake signals only; they may not carry a root,
database path, request body, token, ticket, or proof.

## Electron bootstrap behavior

The single-instance listener is connected before business IPC registration,
backend startup, or window creation. Before any approved request reaches the
Phase-2 coordinator, the broker-only route:

1. performs only required Electron app/session initialization;
2. claims at most one request for that broker turn;
3. does not register business IPC, call `ensureBackendReady()`, or create a
   `BrowserWindow`;
4. invokes the main-owned native folder chooser;
5. on cancel, writes `native_cancelled` with zero settings/Core/project writes;
6. on selection, redeems the one-shot ticket and rechecks canonical root plus
   device/inode;
7. seals the no-replace request-bound binding before starting the exact
   candidate runtime;
8. admits candidate health, POSTs the closed path-free Core command, admits
   active health, repairs desktop settings, then publishes only the path-free
   `library_authority_committed` receipt.

An already running primary instance may drain the spool after a fixed
second-instance wake. That does not start another backend or renderer and does
not itself grant authority.

Desktop settings publication fsyncs the private temp file, renames it over the
settings file, and fsyncs the containing directory. The Phase-0-only callback
is retained as an explicit negative regression:

```text
desktop settings commit succeeds
  -> process crashes before receipt publication
  -> claim remains replayable
  -> restart may re-prompt and recommit the same staged binding
```

That isolated path is not exactly-once. The production Phase-2 coordinator
closes the durable Core side of this gap by sealing the binding before candidate
startup and repairing settings/receipt from the permanent V20 response after a
crash. It still does not claim exactly-once native prompting before the binding
has been sealed.

## Implemented Phase-2 Core completion contract

Phase 2 derives the candidate database only from the verified native root and
uses one Core transaction to register the root, create a
durable scan job, create a deterministic project and minimal unverified
Blueprint, and write an immutable V20 bootstrap receipt.

V20 makes same-request replay permanent, conflicting replay impossible,
and commit-response loss recoverable without duplicate domain writes. The scan
must resume under one job identity. The Blueprint must not fabricate evidence,
Wiki generation, Creator Memory pins, or user semantic confirmation. Editor
handoff remains `awaiting_grounded_timeline` until verified evidence, zero-gap
Coverage, a canonical Timeline, and separate pairing all exist.

### Frozen Phase-2 binding and transaction boundary

The native binding identity is the exact tuple of canonical path, device, and
inode. A rename is an identity change even when device and inode are unchanged;
MemoLens must require a new native confirmation and must not silently migrate
or reuse the path-derived database.

Electron persists one no-replace request-bound envelope under private app
state before candidate startup. The process environment carries only the
opaque `MEMOLENS_BOOTSTRAP_REQUEST_ID`; it does not carry the root, database
path, or a second copy of the binding digest. Candidate startup resolves the
fixed `0700` binding directory and exact `0600`, no-follow, single-link file,
then recomputes the canonical digest from the closed envelope.

The candidate path is deliberately three-stage:

1. migrate the exact candidate database to V20 without registering any root;
2. in one `BEGIN IMMEDIATE`, validate permanent replay, revalidate the held
   directory descriptor and exact path/device/inode, then commit root,
   deterministic project/Brief, queued `library_scan`, minimal unverified
   Blueprint plus its ordinary receipts, and the immutable bootstrap receipt;
3. only after commit, activate the exact runtime and repair desktop settings
   and path-free spool status as projections of Core truth.

`backend-settings.json` and `desktop-settings.json` are not part of the SQLite
transaction. A projection or runtime-activation failure after commit must be
repaired from the permanent receipt; it must not delete Core state or create a
second project/job. The permanent response body is byte-stable across exact
replay. Replay is reported only in an HTTP header and is not added to the
receipt body.

## Implemented Phase-3 scan and Agent projection

The V20 bootstrap receipt's `scan_job_id` is the sole public scan anchor. Core
owns one closed, durable state machine with stable relative-path ordering,
bounded batch commits, explicit child-job admission, root-fingerprint checks,
cancel/interruption/failure states, and exact replay/conflict behavior. Backend
owns the dedicated runner and main-only routes.

`memolens_status` reads the exact V20 database through the safe read-only SQLite
path and projects only the receipt-bound job. The projection is path-free and
closed to status/stage, progress, attempt, cancellation/resume state, six
counts, `no_supported_media`, `eta_seconds: null`, timestamps, and at most an
error code/retryability pair. It never selects the newest or an arbitrary scan
row and never returns a root, path, fingerprint, cursor, checkpoint, or error
detail. Unknown or corrupt receipt/job/checkpoint/error state fails closed.

Focused fixtures validate this contract; they do not constitute a real
user-Library scan-worker, long-running/large-Library, Codex/DeepSeek model, or
packaged-host acceptance journey.

## Functional requirements

- **FR-A2-001**: Codex and DeepSeek Harness expose the same start/status
  contracts and shared implementation.
- **FR-A2-002**: start writes at most one private bounded spool entry and
  performs no backend, database, project, media, or network operation.
- **FR-A2-003**: request, receipt, wake signal, and public projection contain no
  path, database path, device/inode, ticket, token, proof, credential, or
  project payload.
- **FR-A2-004**: the closed request has exactly five fields and no persisted
  idempotency digest; the receipt carries the original `expires_at_ms`.
- **FR-A2-005**: the spool rejects symlinks, non-regular files, oversize input,
  duplicate decoded keys, unknown fields, unsafe modes, identity replacement,
  invalid hardlinks, and bounded-census violations.
- **FR-A2-006**: exact replay returns the existing closed status; conflicting
  artifacts fail closed and never create a `rejected` receipt.
- **FR-A2-007**: before native approval, cold broker-only startup has zero business
  IPC registration, ordinary backend spawn, and BrowserWindow creation. After the
  exact candidate/Core commit, active-runtime proof and settings projection all
  succeed, continue normal companion startup and keep its scan worker alive.
  Cancelled, expired, staged or failed requests never promote to normal startup.
- **FR-A2-008**: only the main-owned chooser can issue a Library ticket;
  cancellation has zero settings or domain writes.
- **FR-A2-009**: the binding is sealed only after one-shot ticket redemption
  and exact path/device/inode revalidation; settings/receipt projections occur
  only after permanent Core commit and active proof.
- **FR-A2-010**: directory approval alone grants no Blueprint semantic,
  Timeline edit, preview, export, provider, deletion, or file-management
  capability; the transaction's minimal Blueprint remains unverified.
- **FR-A2-011**: lack of a proven packaged-app wake path is reported as queued,
  never as an observed dialog.
- **FR-A2-012**: commit-to-receipt replay remains an explicit Phase-0 negative
  regression; the production V20 path repairs from permanent replay but does
  not claim exactly-once native prompting before binding seal.
- **FR-A2-013**: Core bootstrap uses an exact candidate runtime and one
  target-database transaction, never a default ghost Library.
- **FR-A2-014**: permanent replay, scan recovery, project creation, and
  minimal Blueprint are bound by V20 durable authority.
- **FR-A2-015**: editor readiness remains impossible until grounded evidence,
  zero-gap Coverage, canonical Timeline, and separate pairing exist.
- **FR-A2-016**: the durable scan is receipt-bound, root-bound, batch-bounded,
  resumable, cancellable, and rejects stale or conflicting checkpoint writes.
- **FR-A2-017**: Agent scan status is a closed path-free projection from the
  receipt's exact job; rogue jobs and corrupt private state fail closed.

## Still unverified or out of scope

- A real empty or populated user-Library scan-worker journey, including actual
  media import and child analysis completion.
- Grounded Coverage creation, canonical Timeline creation, or editor readiness;
  the controlled empty-Library gate remains
  `canonical_editor_not_editable`.
- A successful canonical-editor credential journey after scan/Coverage/Timeline
  grounding.
- automatic packaged-app/protocol launch not observed in the current checkout.
- model invocation, real native-user acceptance, clean-machine packaging,
  Remote CI, commit, tag, or release.

## Promotion rule

The current status is limited to implemented and controlled-local validation.
After executable/plugin source freeze, the final Codex cache was installed with
source/cache byte parity, the official DeepSeek fresh `web` profile loaded that
exact cache, the production oracle passed 149/149 with zero fail/skip, and
`npm run check` exited 0. Only evidence documentation was written after those
executable/plugin gates, followed by a green `git diff --check`.

Product/host promotion additionally requires the real user-Library scan,
grounded Coverage/Timeline/editor journey, and fresh real Codex and DeepSeek
host/model/native acceptance. A loader inventory, process harness, controlled
fixture, or queued request cannot substitute for those journeys.

The installed-cache and DeepSeek results are loader/configuration evidence, not
a model invocation or native Codex UI journey. Direct cache-root
`test_plugin.py` remains 15/16 because one source-only fixture expects the
repository marketplace outside the installed cache; source tests are 471/471
and both cache/source validators pass. pytest/unittest `ResourceWarning` output
remains non-blocking resource-hygiene debt, not a warning-free lifecycle claim.
