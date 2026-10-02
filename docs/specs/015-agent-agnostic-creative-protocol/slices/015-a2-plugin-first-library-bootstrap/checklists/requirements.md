# Requirements Checklist: ML-015-A2

- Slice status: `PHASE 0-3 IMPLEMENTED / VALIDATED CONTROLLED LOCAL`
- Promotion status: `PRODUCT/HOST ACCEPTANCE AND RELEASE GATES PENDING`

## Phase 0-1 authority, contract, and privacy

- [x] Codex and DeepSeek Harness use the same Agent-neutral start/status code.
- [x] Start writes only one private bounded app-state request and contacts no
  backend, database, project, media, provider, or network surface.
- [x] The request contains exactly schema, kind, opaque request ID, creation
  time, and expiry; it persists no idempotency digest.
- [x] Request, wake signal, receipt, and public projection contain no path, DB,
  token, ticket, proof, device/inode, project, or Timeline payload.
- [x] Public status is closed to `queued_open_memolens`,
  `awaiting_native_confirmation`, `library_authority_staged`,
  `library_authority_committed`, `native_cancelled`, and `expired`, with exact
  `expires_at_ms`.
- [x] Invalid or conflicting input returns a stable error and never creates a
  `rejected` receipt.
- [x] Python publication uses a POSIX directory writer lock, hardlink
  no-replace, file fsync, temp cleanup, and directory fsync.
- [x] Raw census is bounded at 128 and final request/claim/receipt census at 64.
- [x] Python and Electron reject decoded duplicate keys, symlinks, non-regular
  files, oversize files, unsafe modes, unknown names, and identity replacement.
- [x] Python request temps and Electron receipt temps recover only under closed
  naming, size, mode, link-count, and device/inode checks; published `nlink=2`
  temps must match their final inode.
- [x] The Agent cannot choose the root or mint native filesystem authority.
- [x] Electron cancellation has zero settings and domain writes.
- [x] The retained Phase-0 approval callback stages only an exact
  device/inode-verified desktop binding; production approval instead seals the
  exact path/device/inode binding before Core.
- [x] Cold broker-only startup registers no business IPC, starts no Backend,
  and creates no BrowserWindow.
- [x] Desktop settings publication fsyncs temp, renames, and fsyncs the parent
  directory without bypassing the mutation queue.
- [x] Directory approval alone grants no Blueprint semantic, Timeline edit,
  export, provider, deletion, or file-management capability; Phase 2 creates
  only the frozen structural Core facts and an unverified Blueprint.
- [x] The isolated Phase-0 commit-before-receipt fault remains explicitly
  recorded as re-prompt/recommit capable, not exactly-once.

## Controlled-local evidence

- [x] Focused Python bootstrap/MCP/CLI suite passes 67/67.
- [x] Electron cross-language broker/router/authority suite passes 24/24.
- [x] Typecheck and Electron build pass on the implemented Phase 0-1 code.
- [x] Evidence preserves the distinction between focused-local validation and
  installed-host, native-user, production, or release validation.
- [x] Final Codex plugin `0.10.1+codex.20260830154527` is installed; source and
  cache each contain 75 files, checksum dry-run reports zero drift, and both
  validators pass.
- [x] Official DeepSeek Harness HEAD
  `47f943859bef60e4160492346772ded9b24f765a` loads that exact cache through a
  fresh `web` profile and composes the MemoLens root/MCP/skill/prompt layers;
  cache DeepSeek tests pass 6/6.
- [x] Final production negative oracle passes 149/149 with zero fail/skip after
  executable/plugin source freeze.
- [x] Final `npm run check` exits 0: Python 1104/1104, source plugin 471/471,
  Node/renderer 129/129, verify and build gates passed.
- [x] Final evidence-documentation `git diff --check` reports no diagnostic.
- [x] Evidence records the direct cache-root `test_plugin.py` result as 15/16:
  its sole error is the source-checkout-only marketplace fixture, not a claim of
  cache 16/16 or a product/host acceptance result.
- [x] Non-blocking pytest/unittest `ResourceWarning` output remains recorded as
  resource-hygiene debt rather than being hidden or promoted as a clean
  lifecycle proof.
- [x] Evidence explicitly avoids model-call, automatic packaged-app wake, real
  native-user approval, real user-Library scan, grounded editor, Remote CI, and
  release claims.

## Phase 2 durable Core authority

- [x] V20 request-bound durable journal/immutable receipt closes the durable
  commit-to-receipt replay gap and validates permanent replay.
- [x] Exact candidate runtime avoids the default ghost Library and receives
  only the opaque request ID through its environment.
- [x] Root registration, scan job, project, Blueprint, and receipt commit in one
  Core transaction.
- [x] Exact permanent replay repairs active runtime/settings/spool projections
  without duplicating root/project/job/Blueprint writes.
- [x] Minimal Blueprint is explicitly unverified and contains no fake evidence.

## Phase 3 durable scan and Agent projection

- [x] Library scan Core state plus Backend runner/routes pass their final
  focused gate as one closed receipt-bound path.
- [x] `memolens_status` exposes only the exact receipt-bound job's path-free
  state/counts and never a latest or rogue job.
- [x] Unknown/corrupt receipt, checkpoint, error, status/stage, timestamp, or
  count state fails closed without leaking cursor/root/path/error detail.
- [x] Editor remains pending until grounded Timeline and separate pairing; the
  controlled empty-state gate returns `canonical_editor_not_editable`.

## Complete A2 promotion

- [ ] A real empty/populated Library scan-worker journey completes import and
  child jobs without creating fake Coverage or Timeline facts.
- [ ] Grounded Coverage and canonical Timeline permit a separately paired
  editor credential only after all admission requirements exist.
- [ ] Fresh real Codex and DeepSeek host/model/native journeys pass.
- [ ] Clean-machine, Remote CI, commit/tag/release are separately verified.
