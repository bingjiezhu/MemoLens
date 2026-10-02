# Implementation Evidence: ML-015-B2A Canonical Blueprint Workspace & Honest History Bridge

- 证据快照：2026-08-23，本地 shared dirty worktree
- 实施状态：`IMPLEMENTED`
- 验证状态：`LOCAL VALIDATION WITH RESIDUALS`
- 自动化本地 gate：`PASS / EXIT 0`
- admission 残余：`HISTORICAL BASELINE/RED EVIDENCE + VISUAL/DESKTOP + REMOTE CI`
- Remote CI：`NOT RUN`
- 发布状态：`NOT TAGGED / NOT RELEASED`
- 数据库边界：实现没有新增 migration/schema surface；durable no-migration oracle 已证明 V1–V5 managed manifest 和 fresh/v3→v5 normalized schema exact-equal
- 证据规则：本文只记录本次 checkout 实际观察。focused test 不替代 final-diff gate，本次 gate 已单独记录；浏览器只读 smoke 不替代 Electron Desktop authenticated write；实现存在不等于 compiler、unified ledger、render/export 已交付。

## 1. Checkout identity

| Item | Observed value | Status |
| --- | --- | --- |
| Workspace | `<repo-root>` | observed |
| Branch | `main` | observed |
| HEAD | `964f007bc6975cc3b8d4c30b448f49ce09329881` | observed base HEAD |
| Package version | `0.10.1` | observed; no B2A version bump |
| Worktree | large pre-existing + collaborative uncommitted diff | dirty; no commit/push/tag |
| Final diff identity | branch `main`, base HEAD `964f007bc6975cc3b8d4c30b448f49ce09329881`; shared dirty tree showed 132 short-status entries after closure; no commit/push/tag | observed after final gate |
| Writer Python | `/tmp/memolens-b1a-safe.IMMA0V/venv/bin/python` — Python 3.14.2 | observed |
| Linked SQLite | 3.53.4 | observed |
| OS/device | macOS 26.1, arm64 | observed |

## 2. Implemented surfaces

| Contract | Implemented surface | Evidence boundary |
| --- | --- | --- |
| Snapshot-consistent canonical workspace | `BlueprintService.project_workspace` in `backend/src/media/blueprint.py`; transaction-local typed attestation in `core/media_db.py`; project GET wiring in `backend/src/api/routes.py` | one SQLite read transaction and one complete Blueprint revision/operation/receipt audit; the attestation is bound to the exact connection/project/transaction epoch/main-schema name-resolution context and cannot cross a transaction or survive protected schema changes |
| Exact identity | root/current/exact revision `database_uuid`; strict project/head identities | renderer adoption requires captured/current scope and database/project identity |
| Semantic truth | explicit current Blueprint head + existing revision/operation/receipt/schema/digest validators | no legacy/cached fallback on canonical trace/integrity failure |
| Authority projection | full ledger validation, exact current projection, latest bounded safe event window | exact allowlist: `event_id/sequence/authority_operation/observed_revision/decision_units/created_at` |
| Executable truth | server `not_compiled`, `current_timeline=null`, Timeline/Preview/Render/Export capabilities false | no Blueprint→Timeline compiler or binding |
| Semantic history | latest bounded `sequence_desc` operations + totals/truncation | Blueprint-scoped only; not complete project history |
| Legacy history | shared-budget brief/Timeline metadata, exact roles/order, JSON byte preflight | lane corruption degrades only that lane to unavailable |
| Strict renderer contract | `src/blueprint/workspaceTypes.ts`, `workspaceModel.ts`, `src/video/api/creative.ts` | any canonical marker forces closed normalization; only exact legacy discriminator remains compatible |
| Resume/stale response | `src/VideoWorkbench.tsx`, `src/video/projectReducer.ts`, `src/blueprint/BlueprintProjectWorkspace.tsx` | canonical cold resume; child request generation + AbortController + current-head ref and parent scope/database/project checks |
| Workbench | `src/blueprint/BlueprintProjectWorkspace.tsx`, `src/video/video-workbench.css` | 12 semantic sections, 8 authority units, three lanes, compare/review, honest compiler-unavailable state |
| CAS restore | `src/blueprint/workspaceApi.ts`, backend restore route/service | Desktop HTTP requires `expected_database_uuid`; exact DB/head/target, stable command identity, append-only restore + Core refetch |

### Explicitly absent

- No V6 migration, new table/index/trigger/view, compiler manifest, Blueprint→Timeline binding or compiled Timeline.
- No unified cross-domain operation ledger, global replay, undo/redo/branch.
- No MCP write expansion, Agent confirm/revoke, Timeline/render/export/file capability.
- No model/provider call, remote research, media scan or original-file operation in the workspace read path.

## 3. Baseline / red evidence boundary

The historical product break was the prior `creative_blueprint_workbench_unavailable` 409 for a healthy Blueprint-head project. The current shared tree contains the green implementation and regression tests, but no retained command transcript or isolated pre-change worktree proving the red phase. Therefore red-before-green is **not admitted as a preserved artifact**. Phase-0 Tasks T002/T004/T005 remain open: the durable schema oracle covers fresh/v3→v5 rather than the requested pre-implementation fresh/v4→v5 snapshot, and no pre-change B0/B1/B1A/plugin/renderer baseline transcript was retained. Current green results do not retroactively fill those historical evidence gaps.

Exact legacy-only compatibility means a project with zero Blueprint/authority/capability trace and the exact legacy discriminator. Supported pairing/capability issuance requires an existing Blueprint head; if those traces exist while the head is missing, fail-closed behavior is correct and must not be relabeled as a pure legacy project.

## 4. Focused validation

| Scope | Exact command | Observed result | Boundary |
| --- | --- | --- | --- |
| Canonical workspace API | `MEMOLENS_PYTHON=/tmp/memolens-b1a-safe.IMMA0V/venv/bin/python bash ./scripts/run_python.sh -m unittest -v tests.test_blueprint_api` | 15/15 passed | current API slice at capture time |
| Renderer workspace + session | `node --experimental-strip-types --experimental-loader ./tests/ts-extension-loader.mjs --test tests/blueprint_workspace_model.test.mjs tests/video_session.test.mjs` | 18/18 passed in 152.310 ms | current focused run includes latest-window and stale child generation/head oracles |
| TypeScript | `npm run typecheck` | passed | renderer + Electron typecheck |
| Build | `npm run build` | passed | observed before documentation-only closure |
| Relevant B0/B1/B1A + B2A regression | `MEMOLENS_PYTHON=/tmp/memolens-b1a-safe.IMMA0V/venv/bin/python bash ./scripts/run_python.sh -m unittest -v tests.test_blueprint_api tests.test_blueprint_state_machine tests.test_b1_core_ledger tests.test_b1_authority_resources tests.test_b1_backend_protocol tests.test_b1a_verified_prefix tests.test_atomic_media_writes` | 106/106 passed in 127.630 s | captured before the two long B2A profile tests landed; not a final-diff full-suite result |
| SQLite runtime/setup | `MEMOLENS_PYTHON=/tmp/memolens-b1a-safe.IMMA0V/venv/bin/python bash ./scripts/run_python.sh -m unittest -v tests.test_sqlite_runtime tests.test_sqlite_setup` | 26/26 passed in 1.319 s | writer/runtime admission; not itself an exact schema diff oracle |
| Transaction-local attestation regressions | Blueprint API 15, B1 core ledger 13, B1 authority resources 11, workspace/parity/authority/TEMP-name-resolution 8, randomized 200-step 1, CAS 100-round 1, performance 1 | 50/50 passed across the listed focused lanes | exact focused commands/times are recorded in Sections 7–8; authority event/receipt validation remains full-chain |

Python 3.14 emitted existing unclosed-SQLite `ResourceWarning` diagnostics in adversarial tests. Tests passed, but resource management is not claimed solved by B2A.

## 5. Contract and adversarial observations

| Oracle family | Observed result | Admission |
| --- | --- | --- |
| Healthy canonical project | workspace 200 with exact current Blueprint | passed focused tests |
| Canonical trace/integrity corruption | fail closed; no legacy/current fallback | passed focused tests |
| Exact legacy-only discriminator | zero-trace exact legacy stays legacy; unknown/marker-absent lookalikes rejected; missing head with canonical/capability trace fails closed | passed focused renderer/API tests |
| Authority projection | current projection bound to revision; safe event allowlist only | passed focused API/model tests |
| Latest-window integrity | totals/truncation/order/uniqueness and total anchors enforced in renderer | focused model and final full gate passed |
| Legacy projection | brief+Timeline share limit; per-value 1 MiB and aggregate 16 MiB preflight before decode; exact role/order | passed focused API tests |
| Exact revision compare | 12 sections, current and target independently validated | passed focused renderer tests; visual compare observed 12 sections / 2 changed |
| Restore | creates new revision; database/head/target CAS; target authority not copied; same-key replay is stable | passed API/concurrency tests |
| Stale response | scope/database/project mismatch cannot reinsert an old canonical workspace | passed focused renderer tests |
| Blueprint Timeline guard | Timeline mutation/preview/render/export remain false/blocked | passed relevant 106-test run |

## 6. No-migration evidence

| Check | Current observation | Status |
| --- | --- | --- |
| B2A source diff adds migration | none observed | static scope evidence |
| B2A source diff adds schema object | none observed | static scope evidence |
| V1–V5 migration tuple/checksum exact equality | 5/5 tuples admitted by the frozen plugin/Core V5 manifest | passed |
| Managed V5 normalized `sqlite_schema` allowlist exact equality | all 50 frozen managed objects admitted by `validate_exact_v5_schema_manifest` | passed |
| Fresh/v3→v5 schema object set exact equality | 87 normalized objects on each path; new=0, missing=0 | passed |

Durable exact command:

```text
MEMOLENS_PYTHON=/tmp/memolens-b1a-safe.IMMA0V/venv/bin/python \
bash ./scripts/run_python.sh -m unittest -v tests.test_b2a_no_migration
```

Observed result: 1/1 passed in 0.046 s. The normalized full-schema snapshot SHA-256 was `0f265b1c8a868a1ef2944e17fdc3e0d23c844cf624ee1abe0756220c089858f6`. This proves **no B2A migration/schema expansion** for the frozen V5 fixture; persistent compiler/binding work still belongs to B2B/V6.

## 7. Restore and concurrency evidence

Exact focused command:

```text
MEMOLENS_PYTHON=/tmp/memolens-b1a-safe.IMMA0V/venv/bin/python \
bash ./scripts/run_python.sh -m unittest -v \
tests.test_blueprint_state_machine.BlueprintStateMachineTests.test_b2a_seeded_100_round_agent_commit_vs_ui_restore_cas_oracle
```

Observed result: final rerun 1/1 passed in 108.334 s.

| Metric | Observed |
| --- | ---: |
| Seed | fixed by test |
| Rounds | 100 |
| Agent commit wins | 51 |
| UI restore wins | 49 |
| Expected loser conflicts | 100 |
| Same-key lost-response replays | 100 |
| Silent overwrites | 0 |
| Extra replay mutations | 0 |
| Final revision/operation/receipt count | 101 / 101 / 101 |
| Loser-key receipt created | 0 |

The oracle establishes CAS/replay correctness for this fixture; its 108.334 s runtime is not a latency success claim.

## 8. Performance evidence — target passed

Exact focused command:

```text
MEMOLENS_PYTHON=/tmp/memolens-b1a-safe.IMMA0V/venv/bin/python \
bash ./scripts/run_python.sh -m unittest -v \
tests.test_blueprint_state_machine.BlueprintStateMachineTests.test_b2a_workspace_100_by_100_by_100_read_profile
```

| Item | Observed value |
| --- | --- |
| Fixture | 100 Blueprint revisions/operations + 100 authority events + 50 briefs + 50 Timelines = 100 legacy artifacts |
| Environment | macOS 26.1 arm64; Python 3.14.2; SQLite 3.53.4 |
| Seed | `11706624` (`0xB2A100`) |
| Warm-up | 3 reads |
| Measured samples | 25 warm reads |
| Optimization | transaction-local typed ledger attestation; one complete Blueprint revision/operation/receipt audit; authority reuses only exact-snapshot validated head/material while fully auditing authority events/receipts |
| Prior p50 / p95 / max | `1234.727 / 1760.719 / 1934.855 ms` |
| Fixture contract SHA-256 | `ba231276b0cf32be024eb3dadfb1a2ac51343d17a2b78aa09c142287ee011afa` |
| Current p50 | `220.310 ms` |
| Current p95 | `227.083 ms` |
| Current max | `230.360 ms` |
| p95 reduction | `87.1%` (`7.75×` versus the original p95) |
| Current test runtime | 128.910 s |
| Target | p95 ≤ 500 ms |
| Verdict | **PASS** |

Raw samples in measurement order, milliseconds:

```text
[219.017, 221.544, 221.652, 230.360, 220.708, 219.304, 220.038,
 222.334, 220.310, 220.284, 219.779, 219.132, 220.067, 226.167,
 223.751, 223.860, 227.083, 219.645, 220.474, 219.185, 219.372,
 221.315, 221.528, 220.156, 219.593]
```

The test freezes the fixture contract digest, preserves all 25 rounded samples and directly asserts `p95 <= 500.0`. Reuse is deliberately limited to a sealed typed object bound to the exact SQLite connection, project, transaction epoch, total-change count and private issuance registry. Protected ledger reads are main-schema-qualified; DDL/ATTACH/DETACH/relevant PRAGMA statements invalidate the epoch, and protected-name TEMP shadows are rejected before issuance or reuse. There is no cross-transaction/repository/global cache and no caller-supplied boolean validation bypass.

## 9. Visual and accessibility evidence

A temporary synthetic QA database/project and browser page were used; the temporary page was removed afterward. No private media was used. Screenshots appeared in the browser-tool session but were not saved as repo-local artifacts, so screenshot admission remains incomplete.

| View / state | Actual observation | Boundary |
| --- | --- | --- |
| 1440×1000 | canonical project cold-resumed; no page-level horizontal overflow; comparison grid fixed after a real collapse was observed | browser read-only; no archived screenshot |
| 390×844 | one-column compare (304 px), no page-level horizontal overflow; enabled/all interactive controls measured at least 44 px after CSS specificity fix | browser read-only; no archived screenshot |
| Requested 1280×800 | in-app browser actually reported 1280×720; canonical workspace visible, `not_compiled=true`, restore disabled, no overflow | **does not prove 1280×800** |
| Console | fresh 1280×720 tab contained Vite connection + React DevTools informational messages, no errors | one clean fresh-tab observation |
| Compare | earlier browser tab showed all 12 sections with 2 changed | read-only compare only |
| Compiler boundary | legacy Timeline was not current; Timeline/Preview/Render/Export unavailable | observed |

Not completed:

- Real Electron Desktop authenticated writable restore, success refetch and CAS-conflict visual flow.
- Complete keyboard-only revision browse/compare/restore/cancel and focus-return audit.
- Full `aria-live`, color-independent state and every required failure fixture visual audit.
- Repo-local screenshots for healthy, partial authority, restore review/conflict, integrity failure, legacy-lane unavailable, compiler unavailable and legacy-only regression.

Therefore SC-B2A-010 and Tasks T032/T033/T034 remain open even though the inspected layouts had no overflow and the fresh console was clean.

## 10. Repository gates

| Gate | Observed result | Current status |
| --- | --- | --- |
| Focused API | 15/15 passed | passed |
| Latest renderer workspace + session | 18/18 passed | passed |
| Typecheck | passed | passed |
| Relevant 106-test regression | passed before two long profile tests landed | supporting, not final-diff gate |
| 100-round CAS/replay oracle | passed; 0 silent overwrite | passed correctness oracle |
| 100+100+100 performance | p95 227.083 ms ≤ 500 ms; prior 1760.719 ms | **passed hard target** |
| Plugin safe readers / MCP read-only annotations on final diff | 228/228 passed | passed |
| Final-diff `npm run check` | Ruff; 303 Python; 228 plugin; local verify; renderer/Electron build; 87 Node; 59 renderer-model | **PASS / EXIT 0** |
| Remote GitHub Actions | not run | **NOT RUN** |

Final-check record:

- Exact command: `MEMOLENS_PYTHON=/tmp/memolens-b1a-safe.IMMA0V/venv/bin/python npm run check`
- Started/completed time: `2026-08-23T03:26:37-0700` / `2026-08-23T03:34:04-0700`
- Ruff/backend/plugin/local verification/build/Node/renderer counts: Ruff passed; `303/303`; `228/228`; local verification passed; renderer/Electron build passed; `87/87`; `59/59`
- Full-suite B2A profile: p50/p95/max `224.667 / 228.046 / 234.809 ms`, below the same 500 ms hard gate
- Final result: `EXIT 0`
- Final diff identity: branch `main`, base HEAD `964f007bc6975cc3b8d4c30b448f49ce09329881`, shared dirty worktree; no commit/push/tag

## 11. Side-effect boundary

The B2A implementation is a read/UI/restore bridge over existing V5 data and the existing restore command. Source inspection and focused tests observed no new migration, model/provider call, network research, media scan, Creator Memory write, original-file move/copy/delete/upload, FFmpeg invocation or MCP write operation. This is scoped implementation evidence, not a claim that unrelated repository code has no such capabilities.

## 12. Independent review

Three independent review tracks examined architecture/spec consistency, stale-response/race behavior and implementation risks. Their findings led to fixes for canonical resume cancellation, comparison grid collapse, 44 px control specificity, mandatory Desktop HTTP database UUID, stale child response adoption, legacy Timeline resume cancellation, exact legacy discrimination and historical authority binding. Successive transaction-proof reviews then found cross-transaction snapshot reuse, mutable nested decoded values, replacement-forgery despite a recomputed seal, and protected-name TEMP schema shadowing; each was converted into a durable counterexample before closure.

The renderer follow-up review inspected the request-generation/abort/current-head barrier after its last fix and reported `P0=0 / P1=0 / P2=0`. The frozen transaction-proof follow-up ran five lifecycle/immutability/name-resolution cases in 0.303 s and independently reported `P0=0 / P1=0 / P2=0`; a separate read-only query audit found all 26 protected canonical-workspace query sites explicitly `main.`-qualified. Neither review waives incomplete Desktop/visual evidence or the Remote CI boundary.

## 13. Evidence-to-requirement map

| Evidence family | Requirements | Current state |
| --- | --- | --- |
| Canonical workspace exact read | FR-B2A-001–004, SC-B2A-001 | implemented; focused passed |
| Semantic/authority/executable separation | FR-B2A-005–007, SC-B2A-002 | implemented; focused/browser passed |
| Three honest latest-window lanes | FR-B2A-008–012, SC-B2A-003–005 | implemented; focused and final full gate passed |
| Exact DB/head CAS restore and refetch | FR-B2A-013–016, SC-B2A-006 | implemented; 100-round correctness passed; Desktop visual pending |
| Failure closure | FR-B2A-002, FR-B2A-017, SC-B2A-007 | implemented; focused passed; independent review P0/P1/P2 = 0 |
| No migration/capability expansion | FR-B2A-018–020, SC-B2A-008–009 | durable schema oracle and 228 plugin final gate passed |
| Responsive/a11y/honest UX | NFR-B2A-004–006, SC-B2A-010 | responsive browser smoke passed; screenshot/keyboard/Desktop incomplete |
| Local read budget | NFR-B2A-007 | **PASS: p95 227.083 ms (prior 1760.719 ms)** |
| Full local gates | SC-B2A-011 | final `npm run check` passed, exit 0 |

## 14. Current honest conclusion

**B2A is implemented, but it is not fully admitted or released.** A canonical Blueprint project can now reopen in the visual Workbench, preserve semantic/authority/executable separation, show three honest bounded history lanes, compare exact revisions and request append-only CAS restore through the existing Desktop-authenticated path. It still cannot compile a Blueprint into a Timeline, edit/render/export that Blueprint, or claim unified project history.

Current correctness evidence is strong at the focused API/renderer, final full-suite and 100-round CAS/replay levels. The frozen focused local read profile passes at p95 227.083 ms; the full-suite sample passed at p95 228.046 ms; the durable V5 no-migration oracle and final-diff `npm run check` pass. Historical pre-change baseline/red evidence remains incomplete; 1280×800, complete keyboard/focus, authenticated Desktop write and repo-local screenshot evidence remain incomplete; Python 3.14 ResourceWarnings remain; Remote CI was not run. Those are explicit residuals, not footnotes to a completed release.
