# Implementation Evidence: ML-018-A1 Canonical Coverage Plan Baseline

- 证据快照：2026-08-23，本地 shared dirty worktree
- 实施状态：`IMPLEMENTED`
- 验证状态：`LOCAL VALIDATION WITH RESIDUALS`
- 自动化本地 gate：`PASS / EXIT 0`
- Remote CI：`NOT RUN`
- 发布状态：`NOT COMMITTED / NOT TAGGED / NOT RELEASED`
- 证据规则：本文只记录当前 checkout 实际观察。A1 的 deterministic baseline、ledger 与 UI 存在，不等于 Audio Timing、semantic candidate generation、Global Assignment、Timeline lowering、preview、render 或 export 已交付。

## 1. Checkout identity

| Item | Observed value |
| --- | --- |
| Workspace | `<repo-root>` |
| Branch | `codex/next-generation-creator-loop` |
| Base HEAD | `0caa2cf4afcb041150c131a41c4323a907dc6b6e` |
| Package version | `0.10.1`；A1 未做版本发布 |
| Worktree | 大量既有及协作中的未提交变更；未 commit/push/tag |
| Writer Python | `/tmp/memolens-coverage-py314.WIkl3Q/bin/python` — Python 3.14.2 |
| Linked SQLite | 3.53.4 |

## 2. Implemented truth surface

| Boundary | Implemented surface | Honest limit |
| --- | --- | --- |
| Canonical contract | `core/coverage_contract.py` 的 closed Coverage Plan v1、stable IDs、canonical digest、exact Blueprint derivation | 只从 script blocks/material hints 编译；timing 为 `estimated_text`，match 为 `depiction_unspecified` |
| Evidence authority | transaction-local Core resolver 固定 image asset 或 current exact video span proof | unresolved、过短、stale/unavailable evidence 只能成为 alternative/gap，不能 selected |
| Persistence | V6 additive migration；immutable operation/revision/head/receipt；CAS 与永久 idempotency receipt | 不修改媒体原件；V6 migration checksum 为 `cddb91190311db0f5593db6af5788d54f51c2d82385cad4ea6067da51461716e` |
| Trusted reads | exact head join、bounded full-history validation、trusted prefix floor、Blueprint attestation reuse | 没有外部 sealed checkpoint 时，不承诺抵御离线替换整份自洽数据库 |
| Service/API | current/exact/history/freshness GET；Desktop-token materialize POST；稳定 404/409 error mapping | GET/MCP 不获得写权限；数据库路径仍是本地 transport 参数，不进入 Coverage document/response/UI |
| Canonical workspace | project workspace 显示 `missing/current/stale_blueprint/stale_evidence`；损坏 fail closed 为 `coverage_integrity_error` | Coverage current 不使 Timeline/preview/render/export 可用 |
| Renderer | strict types/model/API；materialize/refresh/conflict recovery；Beat/selected/alternative/gap inspection | 不复制 script text，不生成或修改 Timeline |

## 3. Focused final-diff validation

Exact command:

```text
PYTHONWARNINGS=error::ResourceWarning \
MEMOLENS_PYTHON=/tmp/memolens-coverage-py314.WIkl3Q/bin/python \
bash ./scripts/run_python.sh -m unittest -v \
tests.test_coverage_contract tests.test_coverage_persistence
```

Observed result: **40/40 passed in 123.518 s, exit 0**.

| Oracle family | Observed result |
| --- | --- |
| Pure contract | 19/19 passed：closed shape、exact derivation、opaque evidence identity、stable IDs/digest、path leak、short span、duplicate、tamper、replay |
| Persistence/API | 21/21 passed：fresh/current/stale、strict JSON/DB identity、idempotency、CAS、fault rollback、tamper、trusted-prefix rollback、same-UUID replacement、transaction/TEMP-shadow guard |
| Concurrency | 100 rounds completed；each stale-head race had exactly one winner，silent overwrite 0 |
| Fault publication | injected failure after revision insert left operation/revision/head/receipt partial publication 0 |
| Resource lifecycle | same 40-test run passed with `ResourceWarning` promoted to error |
| Blueprint audit reuse | project workspace with materialized Coverage reused one transaction-scoped Blueprint attestation |

Additional focused observations already captured on the same diff family:

- Coverage API + production `create_app` + canonical workspace attestation: 3/3 passed with `ResourceWarning` promoted to error.
- Coverage tamper/receipt repair verification: 3/3 passed.
- Blueprint workspace 100×100×100 profile after Coverage integration: p50 `249.544 ms`, p95 `255.220 ms`, max `255.394 ms`; target p95 ≤500 ms passed. This is a local fixture result, not a general latency claim.
- TypeScript typecheck、renderer/Electron build 与当前 renderer-model suite 均通过；当前最终整仓 gate 观测为 71/71。
- `git diff --check`: passed before final repository gate.

## 4. Schema and compatibility evidence

- V6 is additive and adds only Coverage operations/revisions/heads/receipts plus their indexes/triggers.
- The migration/B1 compatibility lane previously observed 22/22 passed.
- The V6 no-unintended-migration oracle previously observed 2/2 passed with normalized schema SHA-256 `db89d8ba04f2fb707ae562d59972e08e7ee759b681e4356ee65ded3dc8f593af`.
- Blueprint/video compatibility previously observed 61/61 passed. These supporting runs precede the final repository gate and do not replace it.

完整 gate 首轮正确发现两个旧 Creator Memory V2 fixture 未拆除 V6 object/migration，却把 meta 降为 V2；Core 的连续 migration preflight 因而 fail closed。修复只更新 fixture：逆序移除全部 V6 managed objects/migration，再验证 V2→V6。目标测试 2/2 通过，随后完整 gate 也通过；`core/media_db.py` 的 fail-closed 规则未放宽。

## 5. Final repository gate

Exact command:

```text
MEMOLENS_PYTHON=/tmp/memolens-coverage-py314.WIkl3Q/bin/python npm run check
```

| Gate | Final observation |
| --- | --- |
| Ruff | passed |
| Core/unit | 471/471 passed |
| Plugin | 231/231 passed |
| Local verification | passed |
| TypeScript + renderer/Electron build | passed |
| Node/Electron batch | 100/100 passed |
| Renderer models | 71/71 passed |
| B2A 100-round CAS in full suite | passed；silent overwrite 0 |
| B2A workspace performance hard test | passed；p95 ≤500 ms |
| Process result | `EXIT 0` |

The same final code also passed a separately captured `npm run test:node` rerun at Node/Electron 100/100 and renderer models 71/71. The complete default Python suite emitted known `ResourceWarning` from older connection-lifetime fixtures, while the A1/B2B strict focused lane remained warning-clean. The final full gate was not wrapped in a separate aggregate wall-clock timer; component counts, environment and exit status are exact, while no aggregate duration is claimed.

## 6. Side effects and recoverability

A1 performs only `reversible_project_write`: append operation/revision/receipt and CAS the project Coverage head. It does not call a model/provider, access the network, scan new media, invoke FFmpeg, render/export, write Creator Memory, or move/copy/delete/upload original media. Migration creates the existing validated recovery backup before V5→V6 and preserves older tables.

The canonical Coverage document, HTTP response and renderer state are path-free. This does not claim all local HTTP URLs/logs or unrelated legacy code are path-free. SQLite schema/digest/append-only validation catches bounded protected-history inconsistency and rollback relative to the repository-lifetime trusted floor; it is not a cryptographic whole-file anti-rollback mechanism across a fresh process.

## 7. Residuals and non-claims

- Real Electron Desktop authenticated materialize/conflict visual evidence and repo-local screenshots are not captured.
- Remote CI, signed bundle, clean-machine, release and upgrade-channel validation were not run.
- Legacy `core/photo_atlas.py` and older test fixtures still contain connection-lifetime patterns outside A1's production/read/API path; the A1 focused run itself is warning-clean under Python 3.14.
- A1 does not prove semantic relevance, factual support, audio/pause/action timing, global optimality, used-interval planning, continuity improvement, Timeline executability or first-cut quality.

## 8. Current conclusion

ML-018-A1 is implemented and passes its final-diff focused contract/persistence/API/concurrency/fault/resource tests plus the complete local repository gate. It establishes one canonical, append-only Coverage Plan baseline between Blueprint and Timeline, with exact evidence and honest gaps；B2B 现已能从 exact zero-gap A1 baseline 产生 singleton revision-1 Timeline。This is local validation with explicit residuals, not release admission；product V3 remains incomplete until a real preview/edit journey is implemented and validated.
