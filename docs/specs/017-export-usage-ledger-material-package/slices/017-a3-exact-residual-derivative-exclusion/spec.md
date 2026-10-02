# Feature Specification: Exact Residual Material Identity & Export-Derivative Exclusion

- Feature ID：`ML-017-A3`
- 创建日期：2026-08-30
- 实施状态：`IMPLEMENTED / VALIDATED_CONTROLLED_LOCAL`
- 验证状态：`FOCUSED + FIRST FULL LOCAL GATE PASSED / REAL HOST-MODEL PROMOTION BLOCKED`
- 前置：[ML-017-A2 Canonical Usage Projection & Brief Admission](../017-a2-usage-projection-admission/spec.md)
- 计划：[plan.md](plan.md)
- 任务：[tasks.md](tasks.md)
- 证据：[implementation-evidence.md](implementation-evidence.md)

## Why this slice exists

A2 已能精确解释一个 analyzed video segment 的 used union 与 residual intervals，但 `unused_only` 仍只能接纳完整未用 segment：partial-used candidate 的 remainder 没有稳定 identity，不能安全进入 Brief、legacy Timeline 或 canonical Blueprint→Coverage→Timeline。与此同时，successful export output 若被改名、复制或重新导入 Library，当前 raw-material search 仍可能把成片当作 B-roll 再次喂回创作链。

A3 只闭合两件事：

```text
validated successful Usage + current analyzed parent segment
  → exact residual interval
  → deterministic residual identity and binding
  → same-transaction Brief admission
  → legacy/canonical Timeline exact lowering

five-table cold-audited successful Export facts
  → validated output video SHA-256 set
  → backend and plugin raw-search exclusion
```

它不新增可写 `used` flag，不按路径猜 derivative，不把 synthetic residual 写成新的 analyzed segment，也不让 Search 或 Agent 成为 canonical truth。

## Implemented result

- Core 和 standalone plugin 均已实现 deterministic `rseg_*`、closed binding、partial-used residual projection、exact parent/source resolver与 executable-material admission。
- backend/plugin raw search 在各自的单一 SQLite snapshot 内执行五表 census/cold audit，并在 rank 前按 successful canonical output 的 full SHA-256 排除 derivative。
- Director 在同一 `BEGIN IMMEDIATE` 中重读 residual、Usage、source/analysis 与 derivative revision；legacy Timeline 和 canonical Blueprint→Coverage→Timeline 均保留 exact residual lineage。
- V19 只为已有 `output_roots` 表增加 durable output-root identity anchor；它没有新建 mutable residual/derivative 真值表，Usage/Export 仍是业务事实。

## Authority boundaries

- successful canonical Export Revision 及其 immutable Usage occurrences 仍是 usage 与 output digest 的唯一业务事实；preview、failed、cancelled、interrupted 和仅保存项目不产生 derivative exclusion。
- residual 是 current analyzed parent segment、source binding、candidate domain 与 validated Usage projection 的确定性只读派生物。`rseg_*` 不能脱离 parent proof 独立成立。
- Search 只返回 candidate；Director 必须在写 project/Brief 的同一个 `BEGIN IMMEDIATE` transaction 中重算 residual identity、bounds、usage revision、derivative facts 与 source identity。
- legacy Timeline 与 canonical Blueprint/Coverage/Timeline 只能消费 exact residual binding；不得用 preferred source、同文件其他 segment、path 或 caller-provided Timeline 替代。
- Codex 与 DeepSeek Harness plugin 的 SQLite read path 不经过 backend retrieval service，因此必须独立实现同一 fail-closed projection；不能只修 backend 后声称跨 Agent 完成。
- derivative exclusion 是默认 raw/executable-material admission rule，不是隐藏历史。作品历史和 reference-only style view 可以展示成片，但不能把它重新提交为 executable material hint。

## Exact residual identity contract

### Identity

Residual candidate ID 固定为：

```text
rseg_<64 lowercase hex>
```

64hex 是下列 closed canonical JSON 的 SHA-256：

- contract version；
- parent `seg_*` identity；
- exact asset identity 与 full `assets.sha256`；
- pinned source identity/binding；
- current analysis run/revision/input SHA；
- parent candidate half-open domain；
- residual half-open `[source_in_ms,source_out_ms)`；
- current `usage_revision`；
- parent candidate Usage projection digest。

`asset_source_id` 是必须验证的 binding，不单独充当 identity。路径、显示名、rank、host、随机数、wall clock 和 Agent 名称均不得进入 digest。

### Closed `residual_binding`

每个 residual match 必须携 closed `residual_binding`，至少固定：contract version、residual ID、parent segment ID、asset/source identity、analysis binding、parent domain、exact residual interval、usage revision 与 projection digest。缺字段、额外字段、bool-as-int、非 half-open interval、ID/digest 不一致或 parent 不 current 时整个 response fail closed。

Residual 不新增 segment row。Thumbnail、Media detail 与 source read 必须通过 verified parent segment/asset resolver；展示层不得把 `rseg_*` 直接传给只识别 persisted `seg_*` 的旧 resolver。

## End-to-end consumption

### Search and Brief

- backend mixed search 在一个 transaction snapshot 中读取 current candidates、Usage/Export facts、asset SHA 与 derivative set，生成 ordinary 与 residual candidates 后统一 filter/rank/present。
- `unused_only` 可以选择 residual candidate；`usage_selection` 冻结 exact `residual_binding`，不能只提交 `rseg_*` 字符串。
- Director 在同一写 transaction 中先识别 residual identity，再通过 parent resolver 重算；不得先执行 `get_segment(rseg_*)` 后因 synthetic ID 不存在而错误拒绝，也不得跳过 same-transaction admission。
- 新 successful Export、Usage correction、analysis head/source identity 或 derivative facts 变化必须改变 search/usage revision，使旧 selection 返回 stable 409 且 project/Brief/idempotency partial writes 为 0。

### Legacy Timeline

- legacy Timeline lowering 使用 transaction-local exact parent/source resolver，把 residual interval作为实际 source range。
- 不允许 preferred-source fallback、同 asset 其他 current segment、path 重找或扩大回 parent full interval。
- persisted legacy clip 必须同时保留 residual identity/provenance 与 parent `seg_*`/source proof，cold read 可重算 exact interval。

### Canonical Blueprint → Coverage → Timeline

- Blueprint executable material hint 新增 closed `residual_span` proof union；reference-only hint 与 executable hint 必须可区分。
- Coverage 复制并验证完整 residual binding，不把 `rseg_*` 降级成普通 segment ID。
- canonical Timeline 的 source binding 使用 parent persisted `seg_*` 作为 `span_id`，同时固定 exact residual interval 与 residual identity/proof；不得创建不存在的 `rseg_*` database row。
- Export actual-read 与 Usage occurrence 必须使用 exact lowered interval，并能从 canonical history 重放 residual lineage。

## Exact-SHA derivative exclusion

### Authoritative fact set

每次 raw material search 必须在同一 snapshot 内，从 Export operations、jobs、revisions、occurrences、receipts 五表的 project-ID UNION 枚举证据域并 cold audit。只有满足全部条件的 revision 才贡献 derivative digest：

- job terminal state 是 successful；
- revision/receipt/operation/commit attestation/physical output proof 均通过 current validator；
- output role 是 canonical rendered video；
- `video_sha256` 是 validated full lowercase 64hex。

failed、cancelled、interrupted、prepared、published-but-uncommitted、tampered 或 validator-unavailable revision 均不能贡献 exclusion fact；但 tamper/validator-unavailable 必须让当前 raw search fail closed，不能伪装为“没有 derivative”。

### Exclusion semantics

- 对每个 photo/video candidate，以完整 `assets.sha256` 与 validated derivative digest set 比较；同 SHA 改名、复制、换路径、换 source row 仍排除。
- 不使用 filename、path、目录、extension、`final` 前缀、size/mtime 或 perceptual guess 作为 authoritative exclusion。
- raw/default、`unused_only`、`prefer_unused`、`allow_reuse` 与 residual search 均不得提供 `include_derivatives` bypass。
- works/history 与 explicit reference-only style search 可以展示 derivative metadata；任何进入 Brief/Coverage/Timeline 的 executable material hint仍必须拒绝 exact derivative SHA。
- derivative fact revision 必须进入 search revision/admission digest；搜索后新完成的 export 或新导入的 same-SHA asset必须使旧 selection stale。

## Plugin parity

`memolens_mixed_search` 当前走 standalone plugin `memolens_core.py` / `memolens_read_store.py` / `memolens_media_store.py`，不能借 backend 已实现来代替。

- `ReadOnlyMemoLensStore.mixed_material_search` 必须在一个 private read snapshot 中完成 managed-schema validation、五表 census/cold audit、validated successful output SHA projection、photo/video candidate读取、derivative filter、residual derivation、ranking 与 presentation。
- plugin-local Export validator 必须与 Core current schema/receipt/operation/output digest 语义一致；无法验证时返回 stable capability/integrity error，不回落 legacy rows。
- plugin Brief/Blueprint validation 必须理解 exact `residual_binding` / `residual_span`，并拒绝 derivative executable hint。
- Codex plugin 与 DeepSeek Harness adapter继续复用同一 plugin code；不得复制一套 DeepSeek-only business logic。

## User stories and independent tests

### US1：partial-used 视频的真正 remainder 可被选择（P1）

60 秒 parent segment 中 `[10s,14s)` 已 used。`unused_only` 返回两个 exact residual candidates；选择其一创建 Brief 后，Timeline 只读取所选 half-open interval，不扩大到 parent。

### US2：并发 usage/export 不会让旧 residual 偷渡（P1）

Search 后另一个 successful export 使用了选中 residual 的一部分。Create Brief 必须 409，且 project、Brief、idempotency、Timeline 均零部分写入。

### US3：改名或复制的成片不会回到 raw search（P1）

把 successful canonical output 以相同 bytes 不同路径/文件名重新导入，backend 与 plugin raw search 均按 full SHA 排除；failed/cancelled output 的相同 fixture不被错误排除。

### US4：Codex 与 DeepSeek 看到同一候选边界（P1）

同一 frozen SQLite snapshot 下，backend、installed Codex plugin 与 official DeepSeek loader bundle 对 ordinary/residual/derivative candidate IDs、intervals、revision 和 stable errors 完全一致。

## Functional requirements

- **FR-A3-001**：`rseg_*` 必须由 closed canonical facts确定性派生，跨进程/host/Agent 稳定，并对任一 binding/bounds/revision 变化敏感。
- **FR-A3-002**：residual candidate必须携 closed `residual_binding`；synthetic identity不得被写成 analyzed segment row。
- **FR-A3-003**：Presenter/thumbnail/detail必须通过 exact parent resolver，不得把 synthetic ID 当 persisted segment。
- **FR-A3-004**：Director必须在同一 immediate transaction重算 residual 与 derivative facts，stale/substitution时零 partial write。
- **FR-A3-005**：legacy Timeline必须固定 parent segment/source与 exact residual interval，禁止 preferred-source fallback。
- **FR-A3-006**：Blueprint/Coverage/Timeline必须端到端保留 `residual_span` proof，并由 canonical history可重放。
- **FR-A3-007**：derivative set必须来自五表 census + cold-audited successful validated Export，任何 absence claim前完成审计。
- **FR-A3-008**：exact full asset SHA是排除键；rename/copy/path变化不能绕过，path/name heuristic不能替代。
- **FR-A3-009**：failed/cancelled/interrupted output不贡献 derivative fact；tamper或 validator不可用时 raw search fail closed。
- **FR-A3-010**：所有 executable material search/admission无 derivative bypass；reference-only history仍可见但不可执行。
- **FR-A3-011**：backend 与 plugin分别在单一 snapshot实现同义逻辑，plugin不得依赖 backend service存在。
- **FR-A3-012**：search revision必须绑定 Usage、analysis/source与 derivative fact revision，使并发变化产生 stable stale rejection。
- **FR-A3-013**：所有 intervals为整数 half-open，residuals与 used union不重叠且精确覆盖 candidate domain。
- **FR-A3-014**：UI必须显式显示 residual parent与 exact time range，并把 derivative exclusion说明为“successful output bytes”，不声称删除原件。

## Out of scope

- perceptual/near-duplicate derivative识别、转码后视觉相似检测与模型猜测。
- Usage correction/supersession、final/test角色修正、完整素材包与 relink。
- 新的 mutable residual table、materialized Wiki或后台异步 index。
- transition handle、speed/reverse/freeze/nested sequence 等 A2 已声明 unsupported 的非线性 source mapping。
- 删除或移动任何 Library source/output；Remote CI、commit、tag、release。

## Promotion rule

A3 的 controlled-local validation 已由 backend 与 standalone plugin 五表 cold audit、exact-SHA exclusion、residual identity、same-transaction Brief admission、legacy/canonical lowering、两条 production-code vertical、Codex installed-cache parity、official DeepSeek fresh loader 和仓库门禁共同支撑；不由单一 focused test、手工 fixture 或 adapter dump 升级。

`official DeepSeek fresh loader`、Codex cache 安装和 Browser UI 只证明 bundle 装载、代码路径与受控本地界面；它们不等于 Codex/DeepSeek 真实 model tool call、双向 host-model-UI T053/T054、真实用户验收、Remote CI 或 release。这些 promotion blockers 未闭合前，状态只能是 `VALIDATED_CONTROLLED_LOCAL`。
