# Implementation Evidence: ML-015-B2B Deterministic Timeline Lowering

- 证据快照：2026-08-23，本地 shared dirty worktree
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCAL VALIDATION WITH RESIDUALS`
- Focused gate：`PASS / 94 OF 94 / EXIT 0 / RESOURCEWARNING STRICT`
- 原始完整本地 gate：`PASS / CORE 471 / PLUGIN 231 / NODE 100 / RENDERER 71 / EXIT 0`
- 当前最终整仓 gate（含 B2B2/A2）：`PASS / CORE 506 / PLUGIN 231 / NODE 106 / RENDERER 84 / EXIT 0`
- 真实 Electron initial materialize/inspect/export：`RUN / PACKAGE EVIDENCE PRESERVED`；V9 reconcile 未在 fresh Electron 重跑
- Final-fidelity app preview：`NOT IMPLEMENTED`；后续 B2B2 read-only inspection 已实现
- Remote CI：`NOT RUN`
- 发布状态：`NOT COMMITTED / NOT TAGGED / NOT RELEASED`
- 证据规则：本文只记录 shared worktree 中实际观察到的实现与 residual；完整默认测试运行中的既有 `ResourceWarning` 不被写成 warning-clean，新 A1/B2B focused lane 才有 strict warning-clean 证据。

更新说明：本文冻结的是 **initial revision-1 B2B slice** 的证据与原始 94-test gate。当前 worktree 已另行实现 [ML-015-B2B2](../015-b2b2-timeline-reconciliation-inspection/spec.md) 的 V9 deterministic successor 与只读 inspection；因此下文“B2B 不交付 successor/preview”描述的是本切片自身边界，不再代表整个当前 worktree 没有后续能力。

## 1. Checkout identity

| Item | Observed value |
| --- | --- |
| Workspace | `<repo-root>` |
| Branch | `codex/next-generation-creator-loop` |
| Base HEAD | `0caa2cf4afcb041150c131a41c4323a907dc6b6e` |
| Worktree | 有多个既有及并行中未提交变更；B2B 未 commit/push/tag |
| Writer Python / SQLite | `/tmp/memolens-coverage-py314.WIkl3Q/bin/python` — Python 3.14.2 / SQLite 3.53.4 |

最终门禁前必须重读 branch/HEAD/status；不能把本快照当作未来 checkout 身份。

## 2. Observed implementation truth surface

| Boundary | Observed current-worktree surface | Honest limit |
| --- | --- | --- |
| Pure contract | `core/timeline_lowering_contract.py` 定义 closed revision-1/parent-null Timeline document、stable Timeline/track/clip IDs/digest、独立 path-free source manifest、zero-gap 1:1 Beat→clip lowerer、`hard_cut/silent/none` 负能力与 cold replay validator | final-diff strict lane 已覆盖当前 frozen contract；未单独完成 spec 中更宽的 1/3/256-Beat 双运行与逐字段全矩阵，B2B 也不定义 geometry/fps/sample-rate/background |
| V7 ledger | `core/media_db.py` 定义 independent operations/revisions/heads/receipts、additive migration、physical schema manifest、immutable/head guards、initial empty-head CAS、receipt 和 trusted reads | B2B command 仍只允许 first-cut revision 1；B2B2 已用 V9 独立 command 追加受限 successor，但 manual edit/unified history 仍未交付 |
| Service | `backend/src/media/timeline_lowering.py` 在一个 write transaction 中读取 exact Blueprint/Coverage、解析 fixed source、lower/validate 并从 empty head 原子创建 revision 1；read 投影 current/exact-revision freshness | 任何已有 head 一律冲突，没有 origin-aware manual-current 分类；B2B 专项 100-round 与 256-Beat campaign 未单独运行。94-test strict focused lane warning-clean；完整默认 suite 的旧 fixture `ResourceWarning` 仍保留 |
| API/runtime | `backend/src/api/routes.py` 与 `backend/src/__init__.py` 已出现 path-free current/exact-revision Timeline GET、Desktop-authenticated strict first-cut POST、stable errors 与 production service wiring | initial B2B gate 已通过；后续 reconcile POST/history cold audit 由 B2B2 取证，不回写为 B2B 原始范围 |
| Workspace/model | `src/blueprint/timelineTypes.ts`、`timelineModel.ts`、`timelineApi.ts` 与 `BlueprintProjectWorkspace.tsx` 定义 closed shape/cross-projection adoption、explicit materialize/refresh、Timeline revision/content digest、Beat→clip/source inspect、draft/not-approved/not-exportable | Renderer 不重算 Core deterministic IDs/content digest；Timeline 卡片不展示全部 upstream digests；initial Electron materialize/inspect/export 已观察，fresh V9 reconcile/inspection 与完整 responsive/keyboard/focus/ARIA 视觉矩阵未运行 |
| Downstream authority | B2B draft 不授予 render/export root/profile。ML-017-A 可独立消费 exact current Timeline，但必须有自己的 native authority | B2B2 的 read-only inspection 也不建立 final-fidelity preview、approval、export 或 Usage authority |
| Legacy isolation | V7 ledger 不写 legacy `timelines`；Blueprint project legacy create/revise/render guard 与 `historical_observed_context_only` history role 保留 | 整仓回归已通过；未完成 legacy successor 迁移、零 production caller 与可恢复 retirement |

## 3. Closed implemented boundary

- 输入只允许 exact current Blueprint、能从该 Blueprint 重放的 exact current zero-gap A1 baseline Coverage，以及 transaction-local current source bindings；未来 manual/global Coverage 尚不受支持。
- 一个 Beat 恰好生成一个 clip；不重排、重选、合并、拆分、填 Gap 或读取 alternatives 规划新素材。
- video 使用 proof span 左边界与 Beat duration；image 只使用 Timeline duration，不伪造 source interval。
- Timeline document 显式包含 `revision=1/parent=null` 与 exact upstream operation IDs，但不包含 path、UUID、wall-clock、rowid、own operation ID 或 `asset_source_id`；source manifest 另存并固定实际 opaque source identity。
- v1 compiler exact object 为 `memolens.timeline-lowerer/v1 + image_and_video_span + hard_cut + silent + subtitles:none`；clip 固定 `fit=cover/audio_enabled=false`。Timeline output 只固定 duration/aspect ratio，B2B 不交付 geometry、fps、sample rate、background、字幕、转场结构、速度变化或任何音轨能力。
- materialize 是 explicit Desktop `reversible_project_write`；GET 是 path-free read；MCP 继续 `write=false`。
- B2B 只产生可检查 draft，不建立 approval。App preview 留给后续独立切片；ML-017-A export 具有独立 Electron native approval、commit 与 Usage 边界。

## 4. Focused final-diff verification ledger

Exact command:

```text
MEMOLENS_PYTHON=/tmp/memolens-coverage-py314.WIkl3Q/bin/python \
PYTHONTRACEMALLOC=10 PYTHONWARNINGS=error::ResourceWarning \
bash ./scripts/run_python.sh -m unittest \
tests.test_coverage_contract tests.test_coverage_persistence \
tests.test_timeline_lowering_contract tests.test_timeline_lowering_service \
tests.test_timeline_lowering_integration tests.test_timeline_lowering_api \
tests.test_timeline_persistence -v
```

Observed result: **94/94 passed in 132.915 s, exit 0**。其中 A1 Coverage 40/40，B2B Timeline 54/54；同一运行把 `ResourceWarning` 提升为 error。

| Gate | Exact command / artifact | Current observation |
| --- | --- | --- |
| Pure contract/determinism/limits | 上述 strict command | 16/16 passed；另验证不同 span refs 可共享同一 fixed source，而同一 source 绑定不同 asset/digest 仍 fail closed |
| Current-schema fresh DB + V7 catalogue; V6→current through V7/V8 + collision/recovery | 上述 strict command | migration/trigger 6/6 passed；fresh catalogue、recoverable backup、collision/corruption、immutability/head/path guard 均通过 |
| Persistence/CAS/idempotency/fault/tamper | 上述 strict command | integration 11/11 passed；empty-head concurrency、lost-response replay、revision-insert rollback、ledger rollback/tamper 与 source freshness 通过 |
| Service/API/production wiring | 上述 strict command | service 11/11 + API/runtime 10/10 passed |
| Renderer model/API/session | final `npm run check`；另以 `npm run test:node` 复核 exact counts | renderer models 71/71、Node/Electron 100/100、typecheck/build passed |
| 100-round concurrency | A1 100-round CAS included；B2B first-cut concurrency oracle included | A1 100 rounds passed；B2B 的 spec-level 100-round first-cut campaign 未单独运行，不把单一并发测试冒充 100 rounds |
| 256-Beat performance | runtime/fixture/p50/p95/max `TO BE RECORDED` | `NOT RUN IN THIS EVIDENCE` |
| A1/B0/B1/B1A/B2A/plugin/legacy regressions | final `npm run check` | Core/unit 471/471、plugin 231/231、Node/Electron 100/100、renderer models 71/71 passed |
| Final repository gate | `git diff --check && MEMOLENS_PYTHON=/tmp/memolens-coverage-py314.WIkl3Q/bin/python npm run check` | Ruff、local verification、typecheck/build 与全部上述测试通过，exit 0；未记录 aggregate wall-clock |
| Real Electron initial materialize/inspect/export | `<TEMP_EVIDENCE_ROOT>` | initial Timeline→native export success/package observed；该运行使用 schema 8，不能替代 B2B2 V9 reconcile/inspection 验收 |
| Remote CI / tag / release | run IDs/artifacts `TO BE RECORDED` | `NOT RUN` |

## 5. Side effects and recoverability

B2B materialize 只在 exact empty head 上追加一组 operation/revision-1/receipt 并建立 canonical head；不调用模型/provider/network/shell/FFmpeg，不扫描新媒体，不修改 Creator Memory/Blueprint/Coverage/legacy Timeline/原始媒体，也不移动、复制、删除或上传用户文件。V7 是 additive migration；回滚只能关闭 materialize/read capability 并保留 ledger 审计，不能 destructive down-migrate 或把 legacy Timeline 恢复成 current。

## 6. Residuals and non-claims

- B2B2 已实现 muted hard-cut read-only inspection，但 final-fidelity preview 与真实可编辑初剪仍未闭合。
- B2B2 已实现 deterministic Coverage successor revision 与完整 cold history audit；manual edit/restore/rebase/branch/undo/redo 和跨域 unified history仍未实现。
- Gap 填充、global assignment、Technique/Craft、转场、字幕、速度变化与任何音轨能力未实现。
- B2B 不建立 approval/export authority。ML-017-A 的独立 native export code surface 不等于 B2B approved，也不证明 V3 preview/edit journey。
- initial materialize/native export 已有真实 Electron/package 证据；V9 reconcile/inspection、responsive/accessibility、Remote CI、tag/release 仍未验证。

## 7. Current conclusion

Initial B2B pure lowerer、independent V7 ledger、strict materialize/read API 和 canonical workspace inspect 的冻结证据仍为 94/94 strict focused lane。当前 successor 状态见 B2B2 evidence；manual edit、final-fidelity preview、fresh Electron V9 journey 与 Remote CI 未完成，因此仍不能表述为 V3 complete 或 release-ready。
