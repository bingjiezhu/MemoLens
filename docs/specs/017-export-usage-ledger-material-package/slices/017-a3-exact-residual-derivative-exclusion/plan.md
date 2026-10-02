# Implementation Plan: ML-017-A3

- 状态：`IMPLEMENTED / VALIDATED_CONTROLLED_LOCAL`
- 原则：先关闭 derivative fail-open，再开放 residual selection；backend 与 standalone plugin是两个必须同时闭合的生产读面。

## Phase 1：冻结合同与失败 oracle

1. 冻结 `rseg_<64hex>` canonical preimage、closed `residual_binding`、stable errors与资源上限。
2. 冻结五表 census、successful validated output SHA projector和 no-bypass exclusion规则。
3. 先写 rename/copy same-SHA、failed/cancelled/interrupted、orphan/tamper、cross-snapshot与 stale admission失败 oracle。

## Phase 2：backend derivative facts

1. 在 Usage/Export read layer新增 transaction-local validated derivative fact projector，复用现有五表 cold audit，不新增 mutable flag。
2. 将 derivative revision绑定到 mixed-search revision和 Director admission digest。
3. 在 photo/video ordinary与 residual candidate形成前按 full `assets.sha256`过滤；works/reference view维持只读可见。

## Phase 3：standalone plugin derivative parity

1. 在 `ReadOnlyMemoLensStore.mixed_material_search` 建立一个 private snapshot内的 schema validation、五表 census/cold audit、candidate读取、filter/rank/present。
2. 移植/共享 current Export validator语义；无法验证时 fail closed，禁止 legacy fallback。
3. 为 Codex cache与DeepSeek bundle加入 exact candidate/error parity oracle。

## Phase 4：residual identity与Search/UI

1. 在 shared contract模块实现 canonical preimage、ID、binding parser/validator和 interval invariants。
2. backend与plugin从 current parent candidate Usage projection爆炸出 residual candidates；不写新 segment row。
3. Presenter、thumbnail、detail和UI携 parent route identity，显示 exact interval并拒绝 synthetic-ID direct lookup。

## Phase 5：same-transaction Brief admission

1. 扩展 `usage_selection`/Brief provenance以冻结完整 residual binding与 derivative revision。
2. Director在 project/Brief/idempotency同一 `BEGIN IMMEDIATE` 中先解析 residual parent，再重读 current source/analysis/Usage/derivative facts。
3. 覆盖 concurrent Export、analysis head、source replacement、same-SHA import和 idempotency replay；所有冲突零部分写。

## Phase 6：Timeline consumption

1. legacy Timeline使用 transaction-local exact parent/source resolver，不允许 preferred-source fallback。
2. Blueprint material hint新增 closed `residual_span`；Coverage保持完整 binding；Timeline使用 parent `seg_*` + exact residual interval。
3. Export actual-read/Usage cold replay验证 residual lineage，历史 revision不可被 current facts改写。

## Phase 7：vertical与promotion

1. 运行 production-wired controlled-local successful export→same-SHA rename/copy reimport→backend/plugin next-search exclusion vertical。
2. 运行 production-wired controlled-local partial-used parent→residual search→Brief→legacy/canonical Timeline→Export→next residual search vertical。
3. 运行 production inventory/oracle、plugin validator、fresh cachebuster reinstall/source-cache parity、controlled-local Codex Browser、official DeepSeek fresh profile loader、full `npm run check`和全树 whitespace check；分开记录 loader/UI 与仍未完成的 model-call promotion。
4. 独立复审 authority inflation、fail-open absence、snapshot splitting、path heuristic、synthetic identity substitution和UI honesty。

## Schema and rollback strategy

- 实施有意将 schema 从 V18 升到 V19，但只增加 durable canonical Export output-root anchor：`trg_output_roots_identity_immutable` 保护 `id/kind/canonical_path/created_at`，`trg_user_export_output_roots_no_delete` 禁止删除 `user_export` root。
- `permission_fingerprint/status/updated_at` 保持可变，因此当前权限/可用性变化不会改写历史 job/attestation/receipt 所引用的 root identity。
- V19 不新增 residual/derivative 表或 mutable `used/derivative` flag；residual 与 derivative facts 仍是可重建 projection，现有 canonical Export/Usage rows保持 authority。
- 这是对“默认不引入 V19”计划的显式、有限偏离：实现证明五表全缺时的 absence claim/historical cold audit 需要一个不随 mutable permission/status 漂移的 output-root identity anchor。V18→V19 迁移覆盖 preflight name-collision refusal、backup/manifest、checksum、row preservation 与物理 schema digest 验证。
- 任一阶段失败应可通过移除新 read/admission code回到 A2行为；不得回写、删除或迁移现有 export/usage/source history。

## Expected implementation surfaces

- Backend：Usage/Export projector、mixed retrieval/ranking/presenter、Director/Brief admission、legacy Timeline lowering、Blueprint/Coverage/Timeline contracts。
- Plugin：`memolens_read_store.py`、`memolens_media_store.py`、`memolens_core.py`、Brief/Blueprint validators、presenter与对应tests。
- UI/adapters：usage selection types、residual explanation、Codex/DeepSeek shared skill/prompt；不新增第二套 business logic。
