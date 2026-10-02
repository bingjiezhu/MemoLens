# Tasks: ML-015-B2B Deterministic Timeline Lowering

- 状态：`IMPLEMENTED IN SHARED WORKTREE / LOCAL VALIDATION WITH RESIDUALS`
- 完成标记规则：`[x]` 只表示当前 checkout 已有对应代码、测试或文档表面，不代表命令已在 [implementation-evidence.md](implementation-evidence.md) 记录通过。最终验证、视觉与 hosted/release 项在真实观测前保持未勾选。

## Phase 0 — Baseline and red oracles

- [x] **T001** 记录 branch、HEAD、package version、dirty worktree、writer Python/SQLite runtime，保留用户和其他 agent 已有修改。
- [x] **T002** 冻结 V1–V6 migration tuple/checksum、normalized physical schema manifest 和 fresh/V5→V6/V6 快照。
- [x] **T003** 运行并记录 A1 Coverage、B0/B1/B1A/B2A、legacy Timeline/render guard、plugin safe-reader 和 renderer baseline。
- [ ] **T004** 新增并观测 red tests：zero-gap Coverage 仍 `not_compiled`，没有 canonical Timeline materialize/read 表面。

## Phase 1 — Pure contract and deterministic lowerer

- [x] **T010** 冻结 Canonical Timeline document v1 与独立 source manifest 的 closed schema、1 MiB/256-clip limits、compiler contract 和 canonical digests。
- [x] **T011** 实现 timeline/track/clip stable IDs，证明 Timeline bytes 不依赖 UUID、时间、rowid、路径或 execution-local source ID；source manifest 单独绑定 opaque `asset_source_id`。
- [x] **T012** 实现 exact zero-gap Beat→clip hard-cut lowerer：1:1 顺序、exact `start_ms/end_ms`、video left-bound trim、image duration、`fit=cover`、`audio_enabled=false`。
- [x] **T013** 实现独立 validator 与 cold derivation replay，重验 bindings、stable IDs、Timeline/source-manifest digests、Beat/clip timing、`duration_ms/aspect_ratio` 和 exact compiler 负能力。
- [x] **T014** 添加 pure tests：zero-gap 正向、Gap、非 exact binding、unsupported aspect、source-set/proof mismatch、path/unknown field、timing overlap/gap、span/source-lineage 篡改、manifest 顺序及 cold derivation forgery。
- [ ] **T015** 在两个干净运行中重放 1/3/256 Beat fixtures，证明 Timeline document/IDs/content digest exact-equal，并单独记录 relocated source manifest digest 差异。
- [ ] **T016** 补齐并记录显式的 multi-selected、span-too-short、compiler/`audio_enabled`、subtitle/transition/speed-shaped unknown fields 与 256-clip/1-MiB limits 负向门禁。

## Phase 2 — V7 migration and trusted ledger

- [x] **T020** 添加 V7 additive migration：`canonical_timeline_operations/revisions/heads/receipts`、indexes 和 protected triggers。
- [x] **T021** 更新 migration checksum、physical schema manifest、immutable-prefix/trusted-floor 审计，保持 V1–V6 exact bytes/checksum。
- [x] **T022** 添加 current-schema fresh DB 含 V7 catalogue、V6→current 经 V7/V8 与 pre-V7 backup、preflight collision、foreign-key/integrity/rollback 测试文件。
- [x] **T023** 实现 transaction-local fixed source resolver：唯一 preferred，否则 opaque source ID 升序；多 preferred 或无 available source fail closed。
- [x] **T024** 实现 path-free source-binding manifest 及 current/missing/changed/revoked 重验，禁止自动 source failover。
- [x] **T025** 实现 operation/revision/receipt append-only repository API 和 empty-head→revision-1 CAS，四者一事务原子提交；当前 service 不实现 successor head advance。
- [x] **T026** 实现永久 idempotency：same-key same-request replay、key/request mismatch conflict、lost-response 零额外 mutation。
- [x] **T027** 实现 current/exact-revision trusted read、upstream/source freshness 和 stable `timeline_integrity_error`；禁止 legacy/cached fallback，不声称已有 history-list API。
- [x] **T028** 添加 operation/revision/receipt physical immutability、head initial/forward-only/delete guard、revision-insert 故障全回滚、Timeline JSON 篡改和整组 ledger rollback 测试文件。
- [ ] **T029** 在 final diff 上补齐并记录 operation/revision/head/receipt/result/source-manifest/parent 逐层 field-tamper 矩阵，不用现有子集冒充全矩阵。

## Phase 3 — Service, API and workspace

- [x] **T030** 实现 `timeline.materialize_first_cut` service：同一 transaction 内 exact current Blueprint+Coverage、A1 baseline replay、zero-gap、source resolution、lower/validate 和 initial-head CAS。
- [x] **T031** 实现 existing-head-wins：无自动 materialize，任何已有/并发获胜 current 均原样保留，conflict 后零自动写重试；不根据未实现的 manual/restore origin 分类。
- [x] **T032** 新增 current/exact-revision GET 和 Desktop-authenticated materialize POST，强制 expected database UUID、`expected_timeline_head=null` 与 strict JSON；无 history-list GET。
- [x] **T033** 将 canonical Timeline summary/freshness/capabilities 接入 Blueprint workspace 的同一受信读边界，不把 legacy Timeline 升级为 current。
- [x] **T034** 保持 B2B downstream-authority boundary：canonical draft 只提供 path-free exact binding，不授予 preview/render/export root/profile；独立消费者必须自行重验。
- [x] **T035** 当前 B2B 不提供 preview POST 或 app-preview artifact；workspace 明示 preview unavailable，ML-017-A native export 仍是独立 authority。
- [x] **T036** 保持 Blueprint project 的 legacy Timeline create/revise/render hard guard 和 B2A `historical_observed_context_only` history role。
- [x] **T037** 添加 API/production wiring tests：healthy/current/stale/integrity/manual conflict/replay/database switch，以及 legacy/preview-authority 膨胀阻断。

## Phase 4 — Renderer models and UX

- [x] **T040** 添加 `timelineTypes.ts`、closed `timelineModel.ts` 和 `timelineApi.ts`，验证 closed shape、字段形式和 database/project/Blueprint/Coverage/Timeline cross-projection adoption；deterministic ID/content digest 重算仍由 Core 负责。
- [x] **T041** 实现 explicit materialize/refresh session，resume 与 Coverage refresh 不自动编译，stale response 不能覆盖已切换 workspace。
- [x] **T042** 实现 Beat→clip/source inspect UI，显示 hard-cut 限制、Timeline revision/content digest、freshness 和 path-free clip/source identities；当前 Timeline 卡片不展示全部 Blueprint/Coverage digest。
- [x] **T043** 实现 `draft / approval not established / not exportable` 独立状态，不用单一“完成”标签合并；后续 ML-017-A export capability 不回写 B2B approval。
- [x] **T044** UI 明示 B2B preview unavailable；canonical state 只由 Core refetch 推进，任何后续 preview/export artifact 都不成为 Timeline 真源。
- [x] **T045** 添加 model/API/session tests：unknown field、identity mismatch、stale source/response、existing-head 返回 `timeline_manual_current_conflict`、approval/export 误升级和 legacy fallback。
- [ ] **T046** 完成 1440×1000、1280×800、390×844、keyboard/focus/ARIA/触摸目标验收，保存 renderer/browser 读证据与 Electron materialize/inspect 证据。

## Phase 5 — Verification and evidence closure

- [ ] **T050** 运行 100 轮 concurrent empty-head first-cut 竞态、lost-response replay 和全故障注入，确认每项目只有一个 revision-1 winner，silent overwrite/partial publication 为 0。
- [ ] **T051** 运行 256-Beat materialize profile，记录 runtime/seed/raw samples/p50/p95/max 并断言 p95 ≤500 ms（不含 preview render）。
- [x] **T052** 重跑 A1 Coverage、B0/B1/B1A/B2A、migration/integrity、legacy Timeline/render guard、plugin safe-reader/MCP 和 renderer 回归。
- [x] **T053** 在 final diff 上运行完整 `npm run check`，记录 exact counts、runtime identity、base commit、ResourceWarning 和其他 residuals。
- [x] **T054** 执行独立产品/架构/安全复审，覆盖真源、hidden planner、existing-head-wins、source failover、approval/export 误升级、legacy fallback 和 rollback 承诺。
- [x] **T055** 新建并按实际观测收口 `implementation-evidence.md`，记录 final-diff exact commands/counts 与 residuals。

## Explicitly deferred tasks

- [ ] Canonical Timeline manual edit/restore/rebase/branch/undo/redo 与跨域 unified operation history（`ML-015-B2C`）。
- [ ] Coverage Gap 填充、global assignment 与更高阶 planner（`ML-018-B`）。
- [ ] Technique/Craft compiler、transition/subtitle/任何音轨能力（source audio、配乐、旁白、混音）/speed automation（`ML-016/017`）；B2B 只有 source audio disabled 负能力。
- [ ] B2B app preview 与 Timeline approval/master render；ML-017-A 的 native export/usage/package 是独立后续 authority，不视为 B2B 能力。

上述 deferred checkbox 在 B2B 完成时仍保持未勾选；它们是边界记录，不是 B2B 失败。
