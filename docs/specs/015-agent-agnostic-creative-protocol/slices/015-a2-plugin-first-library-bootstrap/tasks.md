# Tasks: ML-015-A2

- Implementation status: `IMPLEMENTED / PHASE 0-3`
- Validation status: `VALIDATED CONTROLLED LOCAL; PRODUCT/HOST PROMOTION PENDING`

## Contract and plugin spool

- [x] T001 Freeze the exact request/receipt schemas, six public statuses,
  `expires_at_ms`, limits, TTL, replay semantics, and stable errors; exclude a
  persisted digest and `rejected` receipt.
- [x] T002 Implement the private request/claim/receipt spool with POSIX writer
  `flock`, exclusive no-follow temp creation, hardlink no-replace publication,
  bounded verified reads, temp/file fsync, and directory fsync.
- [x] T003 Prove empty app-state start writes one `0600` five-field request and
  performs no backend, database, project, media, or network operation.
- [x] T004 Reject symlink, replacement, non-regular, oversize, duplicate-key,
  malformed, expired, unknown, unsafe-hardlink, raw-census, final-census, and
  conflicting-replay inputs.
- [x] T005 Wire shared MCP and CLI start/status operations for both Codex and
  DeepSeek Harness without duplicating business logic.
- [x] T006 Add both actions to the production-surface inventory with only their
  exact app-state write/read effects and no inherited Core authority.

## Electron broker-only and retained Phase-0 route

- [x] T010 Register singleton and fixed second-instance handling before
  business IPC registration, backend startup, and window creation.
- [x] T011 Claim one request per broker turn and enforce one in-process native
  chooser; do not claim cross-crash exactly-once prompting.
- [x] T012 On cancel, write `native_cancelled` with the request expiry and zero
  settings/Core/project writes.
- [x] T013 Preserve the isolated Phase-0 approve callback as an explicit
  `library_authority_staged`/re-prompt negative regression; production approval
  proceeds through the Phase-2 sealed binding and Core transaction instead.
- [x] T014 Prove cold broker-only startup has zero business IPC registrations,
  backend spawns, and BrowserWindow creation.
- [x] T015 Recover strictly verified Python request and Electron receipt temps,
  including post-hardlink `nlink=2`, plus receipt leftovers; fail closed on
  identity mismatch and preserve commit-to-receipt replay as a Phase-0 residual.

## Phase 0-1 focused validation

- [x] T020 Run the focused Python bootstrap/MCP/CLI suite: 67/67 passed.
- [x] T021 Run the Electron cross-language broker/router/Library authority
  suite: 24/24 passed.
- [x] T022 Run TypeScript typecheck, Electron build, focused production wiring
  checks, and scoped whitespace checks; retain their focused-local scope.
- [x] T023 Reinstall the Phase-0/1 Codex cache version
  `0.10.1+codex.20260830135835`, validate it, and prove 280/280 source/cache
  files have no checksum dry-run difference; retain this as reference-only
  because it predates the final Phase-2/3 source.
- [x] T024 Verify the Phase-0/1 snapshot with official DeepSeek Harness commit
  `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e` installs that exact cache into a
  fresh web profile and composes the MemoLens root/MCP/skill/prompt layers;
  retain it as reference-only for the same reason.
- [x] T025 Freeze executable/plugin source, install and validate the final
  Phase-2/3 Codex cache, load that exact cache through a fresh official
  DeepSeek Harness profile, and rerun the production oracle plus full
  repository gates before writing evidence-only documentation. Preserve these
  as controlled-local loader/config and test evidence without
  model/native-user/release inflation.

## Phase 2: V20 durable Core authority

- [x] T100 Add the request-bound V20 durable journal/immutable receipt and full
  migration matrix that closes commit-to-receipt replay.
- [x] T101 Implement the exact candidate runtime and one Core bootstrap
  transaction.
- [x] T102 Remove pre-Core settings publication and default ghost
  Library behavior.

## Phase 3: durable scan and Agent status

- [x] T103 Complete the durable resumable `library_scan`, dedicated Backend
  runner/routes, and path-free progress focused gate.
- [x] T104 Create a deterministic project and minimal unverified Blueprint in
  the V20 transaction without fake evidence.
- [x] T105 Fail closed at Canonical Editor admission until grounded Timeline
  plus separate pairing exist; controlled empty state returns
  `canonical_editor_not_editable`.
- [x] T108 Project only the immutable-receipt-bound scan job through
  `memolens_status`; hide paths/checkpoints and reject corrupt or rogue state.
- [x] T110 Run focused Phase-2/3 evidence: Core 32/32 WAE, Backend scan
  runner/routes 18/18 WAE, adjacent Backend groups 46/46 WAE, final production
  oracle 149/149 with zero fail/skip, editor gate 1/1, and plugin scan status
  18/18.
- [x] T111 Complete `npm run check` with exit 0 after executable/plugin source
  freeze: Python 1104/1104, source plugin 471/471, Node/renderer 129/129, plus
  verify and build gates; complete final `git diff --check` with no diagnostic.
- [x] T112 Install Codex plugin `0.10.1+codex.20260830154527`; prove source and
  cache each contain 75 files, checksum dry-run reports zero drift, and both
  source/cache validators pass. Record the cache-root source-only marketplace
  fixture error separately rather than claiming cache `test_plugin.py` 16/16.
- [x] T113 At official DeepSeek Harness HEAD
  `47f943859bef60e4160492346772ded9b24f765a`, load the exact final cache into a
  fresh `web` profile and prove the root/MCP/skill/prompt configuration plus
  cache DeepSeek tests 6/6; retain loader/config-only scope.

## Phase 4-5 acceptance and promotion

- [ ] T109 Run real empty/populated Library scan-worker, import, child-analysis,
  Coverage, canonical Timeline, and successful editor-admission journeys.
- [ ] T106 Run fresh real Codex and DeepSeek model/host/native journeys.
- [ ] T107 Run clean-machine, Remote CI, commit/tag/release gates.
