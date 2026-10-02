# Implementation Evidence: ML-015-A2

- Evidence date: 2026-08-30
- Checkout: current dirty shared worktree on
  `codex/next-generation-creator-loop`, base HEAD
  `0caa2cf4afcb041150c131a41c4323a907dc6b6e`
- Implementation status: `IMPLEMENTED / PHASE 0-3`
- Validation status: `VALIDATED CONTROLLED LOCAL; PRODUCT/HOST PROMOTION PENDING`
- Release: `NOT COMMITTED / NOT PUSHED / NOT TAGGED / NOT RELEASED`
- Remote CI: `NOT RUN`

## Controlled-local results

| Gate | Current result | Scope and limit |
|---|---:|---|
| Python bootstrap/MCP/CLI focused suite | 67/67 passed | source checkout; shared start/status, strict spool, inventory, CLI/MCP contract |
| Electron cross-language focused suite | 24/24 passed | broker/router/Library authority, Python and Electron temp recovery, strict receipt compatibility |
| Electron Phase-2 coordinator suite | 26/26 passed | sealed binding, candidate/active health proof, Core-before-projection order, crash/TTL replay, zero renderer/indexing dependency |
| Core V20 bootstrap + scan persistence | 32/32 passed with warnings-as-errors | V20 migration, permanent receipt/replay, root/project/Blueprint/scan transaction, checkpoint/CAS/cancel/resume/failure |
| Backend Phase-3 scan runner/routes | 18/18 passed with warnings-as-errors | real temporary SQLite/directory controlled scan, commit faults, child submission, root/symlink/no-media, receipt-bound GET/list, rogue/cancel/resume/replay |
| Backend adjacent production groups | 46/46 passed with warnings-as-errors | production inventory, worker/bootstrap runtime, and image runner; controlled-local integration only |
| Final production negative oracle | 149/149 passed, 0 fail, 0 skip | exact production-surface/authority denial oracle after executable/plugin source freeze; controlled-local only |
| Canonical Editor admission gate | 1/1 passed | controlled empty state is rejected as `canonical_editor_not_editable`; no credential/editor-ready claim |
| Plugin receipt-bound scan projection | 18/18 passed | exact V20 read-only status, closed public shape, Core progress/error-stage parity, rogue-job rejection, corrupt-state fail-closed, legacy compatibility |
| DeepSeek guidance contract | 6/6 passed | server-qualified status flow and explicit loader/model/editor boundary; no model call |
| Codex manifest/guidance contract | 16/16 passed | three default prompts and staged/committed/scan/editor boundary |
| Full source plugin suite | 471/471 passed | final source plugin gate after executable/plugin source freeze |
| Full `npm run check` | exit 0 | Python 1104/1104, source plugin 471/471, Node/renderer 129/129, verify and build passed |
| `npm run build:electron` | passed | Electron TypeScript emission only |
| `npm run typecheck` | passed | renderer and Electron TypeScript checks |
| Final Codex plugin cache | installed and parity-validated | `0.10.1+codex.20260830154527`; source/cache 75 files each, checksum dry-run zero drift, both validators passed |
| Direct cache-root `test_plugin.py` diagnostic | 15/16, 1 error | sole error is source-only `test_marketplace_resolves_from_repository_root`, which expects the repository marketplace outside an installed cache; not reported as cache 16/16 |
| Final DeepSeek Harness loader | passed | HEAD `47f943859bef60e4160492346772ded9b24f765a`, fresh exact-link `web` profile, four MemoLens layers present, cache tests 6/6; no model call |
| Final `git diff --check` | passed | executable/plugin source was frozen before gates; only evidence documentation followed; not a release gate |

These are current-disk controlled-local results in a dirty shared worktree. The
executable/plugin source was frozen before the final cache, loader, oracle, and
full-repository gates; only evidence documentation followed. This is still not
a frozen commit, model invocation, native Codex UI/user journey, real
user-Library full scan, clean-machine result, Remote CI, or production release.

The principal focused command anchors were:

```text
PYTHONWARNINGS=error ./scripts/run_python.sh -m unittest -v \
  tests.test_library_scan_persistence \
  tests.test_library_bootstrap_persistence \
  tests.test_a2_v20_library_bootstrap_migration                 # 32/32
PYTHONWARNINGS=error ./scripts/run_python.sh -m unittest -v \
  tests.test_library_scan_runner                               # 18/18
node --test --test-reporter=spec \
  tests/library_bootstrap_coordinator.test.mjs                 # 26/26
./scripts/run_python.sh \
  .agents/plugins/plugins/memolens/tests/test_library_scan_status.py -q \
                                                                # 18/18
npm run test:plugin                                            # 471/471
npm run check                                                  # exit 0
```

## Implemented contract evidence

- The request schema is closed to `schema_version`, `kind`, `request_id`,
  `created_at_ms`, and `expires_at_ms`. The idempotency key is represented only
  by the derived opaque request ID; no digest field is persisted.
- The public output schema includes exact `expires_at_ms` and only
  `queued_open_memolens`, `awaiting_native_confirmation`,
  `library_authority_staged`, `library_authority_committed`,
  `native_cancelled`, or `expired`.
- Receipts are closed to the four terminal states, carry the original request
  expiry, and never encode a `rejected` outcome. Invalid/conflicting state is a
  stable error.
- Python uses a POSIX directory `flock`, raw census 128, final protocol census
  64, strict no-follow/0600/bounded identity checks, fsynced hardlink
  no-replace publication, and controlled stale-temp recovery.
- Electron rejects duplicate decoded keys, including escaped aliases, and
  recognizes both Python request temps and Electron receipt temps.
- Post-hardlink `nlink=2` recovery verifies that temp and published final share
  the exact device/inode identity before removing only the temp. Unknown or
  unsafe entries continue to fail closed.
- The single-instance listener is wired before business IPC registration. Cold
  broker-only tests prove zero business IPC registration, zero backend startup,
  and zero renderer-window creation.
- Native cancellation creates no settings/domain state. Approval seals a
  no-replace request-bound canonical-path/device/inode envelope before any
  candidate startup.
- Desktop settings publication performs temp-file fsync, rename, then parent
  directory fsync while retaining the serialized mutation queue.
- Candidate startup receives only `MEMOLENS_BOOTSTRAP_REQUEST_ID`, recomputes
  the binding digest from the exact private envelope, and must prove candidate
  identity before the main-only Core POST.
- One V20 transaction commits the active root, deterministic project/Brief,
  minimal unverified Blueprint and ordinary receipts, queued scan job, and
  immutable bootstrap receipt. Exact replay returns the same path-free body;
  conflicting replay fails permanently.
- Active-runtime proof precedes settings projection, and settings/receipt
  failures repair from the sealed binding plus permanent Core replay without a
  second native prompt or duplicate domain writes.
- Core's scan state machine and Backend's dedicated runner use the receipt's
  unique job, stable raw UTF-8 relative-path order, bounded batch commits,
  admitted child jobs, exact checkpoint CAS, cancellation/resume, and
  root-binding checks.
- `memolens_status` exposes only the receipt-bound job's closed, path-free
  state/count projection. A rogue job, corrupt join/checkpoint/error, or
  impossible status/timestamp/count combination fails closed.

## Phase-0 negative regression and Phase-2 closure

The focused fault test deliberately preserves this isolated Phase-0 behavior:

```text
desktop settings commit succeeds
  -> crash before receipt publication
  -> restart sees the replayable claim
  -> native chooser can open again
  -> the same staged binding can be committed again
```

Therefore the isolated Phase-0 callback is not exactly-once. The production
Phase-2 coordinator no longer uses that order: it seals the binding first,
commits/replays the permanent V20 result, proves the active runtime, and only
then repairs settings and the path-free receipt. Focused candidate/active/
projection/receipt crash tests resume the same request without another native
prompt or duplicate Core write. This is durable Core replay evidence, not a
claim of exactly-once native prompting before the binding is sealed.

## Final source-freeze and full-gate evidence

Executable/plugin source was frozen before the final evidence sequence. The
production negative oracle then passed 149/149 with zero failures and zero
skips. `npm run check` exited 0 with Python 1104/1104, source plugin 471/471,
Node/renderer 129/129, and the verify/build gates passing. Only these
evidence-only documents were edited after those runs; the final
`git diff --check` remained green. The older 143/143 oracle and 465/465 plugin
run are superseded checkpoints retained only in history.

The full pytest/unittest runs emitted `ResourceWarning` diagnostics. They did
not change the exit status and are not treated as functional failures, but they
remain explicit non-blocking resource-hygiene debt. These green gates therefore
do not claim a warning-free resource lifecycle.

## Final installed-cache and loader evidence

Codex's app-bundled executable installed `memolens@memolens-local` as
`0.10.1+codex.20260830154527`. The final source and installed cache each contain
75 files. An `rsync -rlni --checksum --delete` comparison reported zero drift,
and both the source and cache validators passed.

Running `tests/test_plugin.py` directly from the installed cache produced
15/16 with one error. The sole failing fixture,
`test_marketplace_resolves_from_repository_root`, derives
`.agents/plugins/marketplace.json` through `PLUGIN_ROOT.parents[3]`; that source
repository premise does not exist above an installed cache. This diagnostic is
not rewritten as cache 16/16. The complete source plugin suite passed 471/471,
while source/cache validation and checksum parity passed independently.

A fresh `DSH_HOME` at
`<TEMP_EVIDENCE_ROOT>` used official DeepSeek Harness
HEAD `47f943859bef60e4160492346772ded9b24f765a`, Node 24.19, and pnpm 11.7.
The `web` profile linked the exact final cache. `--dump-config` contained
`memolens-bundle-root`, `memolens-mcp`, `memolens-skill`, and
`memolens-prompt`, and the cache DeepSeek tests passed 6/6. This proves
loader/config composition only: there was no API key, model invocation, MCP
tool call from a model, Browser editor journey, native Codex UI, or native user
approval.

## Pending product/host promotion gates

- real empty and populated user-Library worker/import/child-analysis journeys;
- grounded Coverage, canonical Timeline, and separately paired successful
  editor admission;
- fresh real Codex and DeepSeek model/host/native UI journeys;
- real native-user cancel/approve/restart acceptance;
- clean-machine packaging, Remote CI, commit, tag, and release.

## Current non-claims

- no real user-Library full scan, production indexing, or long-running/large-
  Library validation;
- no grounded Coverage or canonical Timeline creation from that scan;
- no editor-ready or successful editor-credential claim; the focused empty
  state is correctly blocked as `canonical_editor_not_editable`;
- no general exactly-once native-prompt claim before sealed confirmation;
- no stable packaged-app automatic wake claim;
- no Codex or DeepSeek model tool invocation;
- no real native-user journey or clean-machine acceptance;
- no Remote CI, commit, tag, or release.
