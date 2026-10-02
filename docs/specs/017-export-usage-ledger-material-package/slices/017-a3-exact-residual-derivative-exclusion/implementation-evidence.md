# Implementation Evidence: ML-017-A3

- 证据快照：2026-08-30
- checkout：branch `codex/next-generation-creator-loop`，base HEAD `0caa2cf4afcb041150c131a41c4323a907dc6b6e`；证据来自当前 dirty shared worktree。
- 实施状态：`IMPLEMENTED / VALIDATED_CONTROLLED_LOCAL`
- 发布状态：`NOT COMMITTED / NOT TAGGED / NOT RELEASED`
- Remote CI：`NOT RUN`

## Implemented contract

- deterministic `rseg_<64hex>` exact residual identity、closed `residual_binding`、strict parser/limits 与 parent-resolved source/detail route。
- backend 与 standalone plugin 各自在一个 SQLite snapshot 内执行 Export operations/jobs/revisions/occurrences/receipts 五表 census/cold audit，投影 successful validated output SHA，并在 rank 前对 photo/video raw candidates 执行 exact-SHA exclusion。
- `derivative_revision` 进入 Search/Brief admission identity；Director 在 project/Brief/idempotency 同一 `BEGIN IMMEDIATE` 中重读 source、analysis、Usage、residual 和 derivative facts，stale/substitution 零部分写。
- legacy Timeline 使用 exact parent/source resolver；canonical Blueprint→Coverage→Timeline 通过 `residual_span` 保留 parent `seg_*`、exact half-open interval 和 residual proof，不写 synthetic segment row。
- Export actual-read/Usage cold replay 保留 residual lineage；历史 revision 不由 current Usage/derivative facts 改写。`BlueprintService.project_workspace()` 在读 current executable materials 时重新验证 derivative authority，但显式 historical revision 仍可读，不把 current rejection 改写进历史。
- UI 显示 residual parent/exact range，并把 raw-search exclusion 说明为 successful output bytes 的 exact-SHA exclusion，不声称删除原件。

## Intentional V19 deviation

计划原本默认不增 schema，但五表全部缺席时的 absence claim 与 historical cold audit 需要一个不随当前权限/状态漂移的 durable output-root identity，否则删光证据域可以失败开放。因此实施显式引入 V19：

- `trg_output_roots_identity_immutable` 禁止修改 `id/kind/canonical_path/created_at`。
- `trg_user_export_output_roots_no_delete` 禁止删除 `user_export` root；`app_preview` root 仍可删除。
- `permission_fingerprint/status/updated_at` 仍可修改；历史 job/attestation/receipt 保留当时 fingerprint，但当前 mutable 状态不参与 durable root identity。
- V18→V19 验证覆盖 pre-migration backup/manifest、migration checksum、原有 rows 不变、物理 schema digest 和 trigger 同名冲突在 backup 前 fail closed。

V19 没有新增 residual/derivative 可写表，也没有 mutable `used/derivative` flag；canonical Export/Usage 仍是业务事实，residual/derivative 仍是可重建 projection。

## Focused and repository validation

| Gate | Observed result | Status |
| --- | --- | --- |
| A3 Core warning-as-error focused suites | 79/79 | PASSED |
| A3 exact-SHA UI wording/structure focused suite | 9/9 | PASSED |
| Current packaged renderer-model suite after adding the 9 UI tests | 129/129 | PASSED |
| Standalone plugin A3 suite at A3 closure | 429/429 | PASSED |
| Production negative oracle | 141/141 (`failed=0`, `skipped=0`) | PASSED |
| First full `npm run check` on its then-current diff | Core 1043/1043; plugin discovery 432 tests passed; renderer-model 120/120; command exit 0 | PASSED |
| Independent review | P0=0 / P1=0 | PASSED WITH ONE NONBLOCKING TEST RESIDUAL |
| Final current-diff full repository rerun after all later code merges | Not yet backfilled in this snapshot | PENDING ROOT TASK |

独立复审的唯一非阻断 residual 是：Core 尚无一条与 plugin 对称的“显式 stale historical restore target”单独测试；现有 resolver 排序与 cold-replay tests 已约束该路径，因此不是 P0/P1 实现缺陷。

## Controlled-local verticals

- partial-used parent 产生 exact residual candidates，选择后通过 Brief admission、legacy Timeline 和 canonical Blueprint→Coverage→Timeline，Export actual-read/Usage 只消费所选 half-open interval，且无 `rseg_*` persisted row。
- successful canonical output 以 same bytes 改名/复制/重新导入后，backend 与 standalone plugin 均按 full SHA 排除；failed/cancelled/interrupted 不误排，tamper/validator-unavailable fail closed。
- Search 后并发 Export、Usage、analysis/source 或 same-SHA import 变化会改变 admission identity，旧 selection 稳定冲突且 project/Brief/idempotency/Timeline 零部分写。

这些 vertical 使用 production code 与真实 SQLite/file-byte 证据，但仍是 controlled-local test journeys，不是真实用户、生产或模型验收。

## Codex install and DeepSeek loader evidence

- fresh cachebuster 版本：`0.10.1+codex.20260830121850`。
- installed cache：`<codex-cache>/memolens-local/memolens/0.10.1+codex.20260830121850`；source↔cache checksum parity 无差异。
- cache 内 safe playback/editor/DeepSeek 子集在显式 source `PYTHONPATH` 下通过 55/55；这是 installed-byte path 验证，不是 Codex model tool call。
- official DeepSeek Harness checkout commit `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e` 的 fresh bundled profile 装载并启用 `memolens-bundle-root`、`memolens-mcp`、`memolens-skill`、`memolens/prompt`，且 MCP child 实际从上述 cache 启动。该 profile 没有 API key，未运行 DeepSeek model call 或 A3/AAC editor journey。
- controlled-local Codex Browser 验证了 shared Canonical Editor 的 Split/Save/Remove/Discard 路径；A3 exact-SHA 解释由 9/9 UI 结构测试覆盖。不把两者合并写成 A3 真实 model-driven 旅程。

## Residuals and non-claims

- 未运行 Codex/DeepSeek 真实 model tool call、fresh 双向 host-model-UI T053/T054 或真实用户验收。A3 task T053 是 Export actual-read/Usage lineage 验证，不是 B2B4 的 real-host T053。
- 未运行 Remote CI、clean-machine acceptance、commit、tag或 release。
- 不声称 perceptual/near-duplicate、转码后视觉相似、Usage correction/supersession、final/test 角色修正、完整素材包或 relink 已覆盖。
- exact-SHA exclusion 不删除、移动或隐藏原件；works/history/reference-only 可见性仍保留，只禁止 derivative bytes 重新进入 executable-material 链。
