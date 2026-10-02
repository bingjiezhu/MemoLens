# Feature Specification: Deterministic Timeline Lowering

- Feature ID：`ML-015-B2B`
- 创建日期：2026-08-23
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCAL VALIDATION WITH RESIDUALS`
- 授权依据：用户于 2026-08-23 授权按产品目标调整并持续实现必要切片
- 父规范：[ML-015](../../spec.md)
- 前置：[ML-015-B2A Canonical Blueprint Workspace](../015-b2a-canonical-blueprint-workspace/spec.md)；[ML-018-A1 Canonical Coverage Plan](../../../018-script-coverage-global-footage-assignment/slices/018-a1-canonical-coverage-plan/spec.md) 已实现并在本地验证，但仍保留其实施证据中的 residuals
- 后续：[ML-015-B2B2 Timeline reconciliation & read-only inspection](../015-b2b2-timeline-reconciliation-inspection/spec.md) 已实现受限 deterministic successor/V9 history/inspection；`ML-015-B2C` unified project history/undo；`ML-016` craft compiler；[ML-017-A](../../../017-export-usage-ledger-material-package/slices/017-a-canonical-export-usage-ledger/spec.md) 已完成独立 native render/export/usage 的本地验证并保留 residuals，不回写 B2B approval
- 实施证据：[implementation-evidence.md](implementation-evidence.md)

## Why this slice exists

MemoLens 现在已有两个独立真源：Creative Blueprint 表达创作意图，Coverage Plan 表达每个 Beat 选了什么素材、还缺什么。当前 `timelines` 表及 Timeline 1.0 是 legacy 实现：ID 和时间元数据可变，只固定 legacy brief revision，没有 exact Blueprint/Coverage binding、canonical head 或独立 receipt ledger。它不能被直接重命名为新真源。

B2B 只完成一次机械 lowering：

```text
exact current Creative Blueprint
  + exact current zero-gap A1 baseline Coverage Plan
  + transaction-local fixed source bindings
  → deterministic hard-cut Timeline draft
  → inspect in the canonical workspace
```

它不重新选材、不改写 Coverage、不填 Gap、不生成转场/字幕或任何音轨编排，也不把 draft 升级为用户批准或可导出成果。v1 clip 明确固定 source audio disabled；这只是负能力声明，不代表 B2B 交付了任何音轨或 preview/render 能力。

## Authority and truth boundaries

```text
Blueprint        = 表达什么、面向谁、输出约束
Coverage Plan    = 每个 Beat 使用哪个 verified assignment
Timeline draft   = 如何把已选 assignment 机械排成执行指令
Approval/export  = 后续独立 authority；B2B 不推导
```

- **Exact current** 意味着 Core 在一个受信任读边界中校验 explicit Blueprint head、explicit A1 baseline Coverage head、两者 exact binding 与 baseline derivation；客户端自报 digest 不是信任来源。
- **Lowerable Coverage** 必须是 `current`，且能从 exact Blueprint 重放为当前 ML-018-A1 deterministic baseline；每个 Beat `gap=null`，恰好有一个 selected assignment，时间槽连续且总时长与 output binding 一致。任何 Gap 都是显式阻断，不是黑帧或无关素材的授权。未来 manual/global Coverage 即使 zero-gap，也不因当前 B2B 自动获得 lowering 支持。
- **Canonical Timeline draft** 是 V7 独立领域文档和 ledger，不写入 legacy `timelines`。legacy Timeline 永远只是 `historical_observed_context_only`。
- **Existing-head-wins** 意味着 B2B 只允许在 canonical Timeline head 明确为空时创建初始 revision 1。任何已有 head（无论未来由何种编辑/恢复链路产生）都必须保留并返回 `timeline_manual_current_conflict`；当前切片不实现 successor revision、手工 edit、restore 或 rebase。

## User stories

### US1：把已完整分配的 Coverage 物化为稳定 hard-cut draft（P1）

用户在 Coverage 没有 Gap 时点击“生成初剪”，系统按 Beat 顺序一对一生成 clip，保留 exact Blueprint/Coverage/source binding。

**Independent test**：为 3 个连续 Beat 提供 image/span/image 的 verified selected assignments，对同一 exact 上游绑定重放 lowerer，并只改变 opaque `asset_source_id` 与 resolver 输入顺序；验证 Timeline bytes/Timeline-track-clip IDs/content digest exact-equal，source manifest digest 则诚实绑定实际 source identity，clip 顺序与起止与 Beat 一致。

**Acceptance scenarios**：

1. **Given** exact current Blueprint 和 zero-gap Coverage，**When** materialize，**Then** 一个 Beat 恰好生成一个 clip，不重排、合并、拆分或换材。
2. **Given** video proof 比 Beat 长，**When** lowering，**Then** clip 固定为 `[proof.start_ms, proof.start_ms + beat_duration)`；这是带版本的机械规则，不是二次内容选择。
3. **Given** image assignment，**When** lowering，**Then** image 的 Timeline 时长精确等于 Beat 时间槽，不伪造 source in/out。

### US2：任何未解决缺口或变旧绑定都失败关闭（P1）

用户不会得到一条被隐式填满的 Timeline。Core 必须在提交前重读 exact heads、验证全部 ledger，并为每个 selected assignment 固定当前可用 source。

**Independent test**：分别构造 Coverage Gap、多 selected assignment、stale Blueprint、stale Coverage evidence、missing/changed source 和伪造 source ID，验证所有情况都在零 V7 部分写入下失败。

**Acceptance scenarios**：

1. **Given** 任意 Beat 存在 Gap，**When** materialize，**Then** 返回 `coverage_not_lowerable`，不生成黑幕、占位 clip 或随机素材。
2. **Given** Coverage 固定的 Blueprint 不再是 current，**When** materialize，**Then** 返回 stale/conflict，不用新 Blueprint 配旧 Coverage。
3. **Given** 一个 source binding 物化后变成 missing/changed/revoked，**When** read 或下游消费者申请执行，**Then** Timeline 仍可审计地读取但显式 `stale_source_binding`，任何 preview/export adapter 都不得自动切换到另一路径。

### US3：重试、并发和故障不会覆盖 current（P1）

首次物化命令必须明示 `expected_timeline_head=null` 并使用永久 idempotency receipt。相同请求只有一次领域变化；任何已有 head、并发首次物化或故障都不会被 compiler 静默覆盖。

**Independent test**：运行 100 轮 empty-head concurrent first-cut materialize、same-key lost-response replay 和 operation/revision/head/receipt 各提交点故障注入，验证每个项目只有一个 revision-1 winner，silent overwrite 和 partial publication 都为 0。

### US4：用户可以检查 draft，但不会被误导为已批准（P1）

工作台显示 exact Timeline revision/content digest、clip/Beat 对应、opaque source identity、freshness 和 hard-cut 限制；Blueprint/Coverage 详细 binding 由 Core resource 保留，当前 Timeline 卡片不声称展示全部 upstream digest。B2B 不因 draft 存在而开放 preview、approval 或 export；后续 preview 或 ML-017-A export 只能通过各自独立、重新验证 exact current/source-current binding 的 capability 与 authority。

**Independent test**：从空 Timeline 工作台物化 draft，检查 1:1 Beat/clip 映射；再使 source stale，验证 B2B 仍可审计读取、所有执行 capability 关闭且 UI 不显示“已批准”。ML-017-A 的 native export capability 必须作为独立后续测试，不能由 B2B 状态推导。

## Functional requirements

- **FR-B2B-001**：Canonical Timeline 必须是独立、closed、版本化领域文档；不得以 legacy `timelines.timeline_json` 、renderer state 或 preview artifact 作为新 current 真源。
- **FR-B2B-002**：一次 materialize 必须在同一 SQLite write transaction 中取得并验证 exact current Blueprint head、exact current Coverage head、Coverage→Blueprint binding、A1 exact-baseline derivation 及明示为 `null` 的 Timeline head；不接受客户端提供的 Blueprint/Coverage document。
- **FR-B2B-003**：Timeline document 与 persistence row 必须固定 exact Blueprint `{revision,content_sha256,semantic_sha256,operation_id}` 和 exact Coverage `{revision,content_sha256,evidence_manifest_sha256,operation_id}`；Coverage 内嵌 Blueprint binding 必须 exact-equal。
- **FR-B2B-004**：Lowerer 不得检索、打分、排序、重选 assignment 或读 `alternatives`来推导替代素材；它只消费已验证 selected assignment。
- **FR-B2B-005**：只有 exact A1 baseline derivation 通过，且每个 Beat `gap=null` 并恰好一个 selected assignment 的 Coverage 可 lowering。非 baseline/manual/global Coverage、零个或多个 assignment、重叠/不连续 timing 或 total-duration mismatch 均必须 fail closed，直到后续切片明确扩展合同。
- **FR-B2B-006**：Beat 顺序、`start_ms/end_ms`、assignment 和 clip 必须 1:1；不得丢弃、合并、拆分、重排或在 Beat 之间新增时间。
- **FR-B2B-007**：video clip 必须使用 Coverage proof 的 exact asset/span identity，source range 固定为 `[span.start_ms, span.start_ms + beat_duration)`；image clip 无 source range，duration 固定为 Beat duration。
- **FR-B2B-008**：Core 必须为每个 assignment 在交易内解析一个当前 `available` source：先按受信仓库规则选唯一 preferred source，否则按 opaque `asset_source_id` 升序取第一个。选中后必须固定，读取或任何下游执行不得自动 failover。
- **FR-B2B-009**：独立 Source Binding Manifest 必须按 clip 顺序一对一固定 `clip_id/evidence_ref/asset_id/asset_sha256/asset_source_id/coverage_proof_sha256/media_kind`；video 另固定 span/analysis/source-range lineage。不得保存或响应 path、root path/ID、relative path、filename、file ID、mtime、凭证或用户文本。
- **FR-B2B-010**：Timeline、track 和 clip IDs 必须从 project/Beat/assignment/evidence/media lineage 等闭包输入确定性导出，不得使用 UUID、wall-clock 或数据库 rowid。Source manifest 不另造一个不存在的 binding ID。
- **FR-B2B-011**：Canonical Timeline v1 document 显式包含 `revision=1`、`parent=null` 和 exact upstream operation IDs，但不包含 own operation ID、created-at、wall-clock、本地 path 或 `asset_source_id`；`timeline_content_sha256` 是该 closed document 的 canonical SHA-256。只改变 resolver 输入顺序或 opaque source ID 不得改变 Timeline bytes，但必须改变独立 source-bindings digest。
- **FR-B2B-012**：Compiler object 必须 exact-equal `{id:"memolens.timeline-lowerer/v1",media:"image_and_video_span",transitions:"hard_cut",audio:"silent",subtitles:"none"}`。这是 hard-cut/silent 负能力合同，不表示音轨或 preview/render 已实现。
- **FR-B2B-013**：v1 只产生一条 `primary_visual` track 和连续 clips，每个 clip 固定 `fit=cover` 与 `audio_enabled=false`；不包含 transition list、text/audio track、subtitle、speed ramp 或混音结构。
- **FR-B2B-014**：Timeline `output` 只固定 Coverage 的 exact `duration_ms` 与受支持的 `aspect_ratio`；B2B 不定义 pixel geometry、fps、audio sample rate 或背景颜色。这些 render/profile 决定属于独立下游，ML-017-A 使用自己的 1080p/30 fps/silent 合同。
- **FR-B2B-015**：纯 validator 必须验证 closed document/source manifest、stable IDs、Beat/clip timing、compiler/output 负能力与每 clip source lineage；cold audit 必须从 exact Blueprint/Coverage/source inputs 完整重放并比对 Timeline 与 source manifest，持久化行不能自证正确。
- **FR-B2B-016**：V7 必须以 additive migration 新增 `canonical_timeline_operations`、`canonical_timeline_revisions`、`canonical_timeline_heads`、`canonical_timeline_receipts` 及必要 index/immutable triggers；不得改写 legacy `timelines` rows。
- **FR-B2B-017**：operation/revision/receipt 只 append，head 不可删除；当前 `timeline.materialize_first_cut` 只从 exact empty head 创建 revision 1。operation、revision、head、receipt 必须在一个 `BEGIN IMMEDIATE` 事务中原子提交；V7 的 forward-only head trigger 不等于 B2B 已交付 successor command。
- **FR-B2B-018**：idempotency receipt 必须永久绑定 database/Desktop principal/project/command/exact Blueprint/Coverage/`expected_timeline_head=null`/request digest/result。same key+same request 返回同一结果；same key+different request 稳定 conflict。
- **FR-B2B-019**：物化不得自动运行、自动写重试或在 conflict 后重算。任何 current canonical head 存在时，必须原样保留并返回 `timeline_manual_current_conflict`；当前实现不根据一个尚未存在的 manual/restore origin 分类来决定是否覆盖。
- **FR-B2B-020**：Current/exact-revision read 必须验证 V7 ledger、closed document/source manifest、digests、exact upstream bindings 和 source freshness；成功投影只区分 `missing`、`current`、`stale_blueprint`、`stale_coverage`、`stale_source_binding`。当前无 canonical Timeline history-list API 或 `manual_current` freshness state。任何 ledger/contract 损坏稳定 fail closed 为 `409 timeline_integrity_error`。
- **FR-B2B-021**：任何下游 preview/render/export adapter 只能消费 exact current、source-current 的 canonical Timeline revision，必须在执行前重验 source/content binding，且不得把 artifact 反写成 Timeline 真源。B2B 当前不交付 app preview；未来 preview 是可恢复 cache effect，不是 export 或 approval。
- **FR-B2B-022**：B2B 产出的 Timeline 始终投影为 `draft`、`approval=not_established`、`exportable=false`。Blueprint 8/8 authority、zero-gap Coverage 或任何下游 preview/export success 都不得回写为 B2B 用户批准。
- **FR-B2B-023**：Canonical workspace UI 必须提供 materialize/refresh、Timeline revision/content digest、freshness、Beat→clip 和 opaque source inspect；B2B 自身不开放 preview/export/publish，也不把未展示的 upstream digest 伪称为用户已检查。后续独立 capability 只有在 server 重验 exact binding 后才能出现。
- **FR-B2B-024**：GET read 保持本地只读表面；materialize POST 必须经 Desktop token、expected database UUID 和 exact head preconditions。MCP 保持 `write=false`，paired CLI 不自动获得 Timeline/preview/export capability。
- **FR-B2B-025**：既有 legacy Timeline 只保留在 B2A history lane；不迁移、导入、合并、修订或渲染为 canonical current。Blueprint project 的 legacy create/revise/render guard 必须继续生效。
- **FR-B2B-026**：B2B 不调用模型、网络、shell 或自由 FFmpeg，不扫描新素材，不修改 Creator Memory/Blueprint/Coverage/原始媒体，不移动、复制、删除或上传用户文件。
- **FR-B2B-027**：V7 migration step 必须保持 V1–V6 migration bytes/checksum，更新 closed physical schema manifest 和 trusted immutable-prefix 验证，并提供 current-schema fresh DB 含 exact V7 catalogue、V6→current 经 V7/V8 与 pre-V7 backup、collision/recovery、tamper 和 rollback 证据。

## Canonical entities

- **Canonical Timeline Document v1**：closed first-cut document；包含 `revision=1/parent=null`、timeline/track/clip IDs、exact upstream bindings、compiler、`duration_ms/aspect_ratio` 和 primary visual track，不内嵌 execution-local source manifest。
- **Canonical Timeline Revision Row**：append-only persistence envelope；另固定 document/source-manifest digests、own operation ID 和 created-at。当前 B2B 只写 revision 1。
- **Canonical Timeline Head**：项目唯一 current pointer；当前 first-cut command 只做 empty→revision-1 CAS。
- **Timeline Operation/Receipt**：定位 actor、command、preconditions、request/result digest 与丢失响应重放的不可变证据。
- **Source Binding Manifest**：Coverage assignment 到某一 opaque、当前可用 asset source 的无路径固定映射。
- **Timeline Freshness**：Timeline 相对 current Blueprint、Coverage 及 fixed source bindings 的可执行新鲜度状态；不等于 app preview、approval 或 export authority。

## Non-functional requirements

- **NFR-B2B-001 — Determinism**：冻结 project/upstream/evidence 输入重放三次，Timeline document/IDs/content digest exact-equal；只改变 opaque source ID 时 Timeline 不变，source-bindings digest 必须随之变化。
- **NFR-B2B-002 — Atomicity**：任意故障点后 operation/revision/head/receipt 的可见状态必须是全旧或全新，不存在部分 publication。
- **NFR-B2B-003 — Boundedness**：单个 Timeline document 最多 256 clips，Timeline/source-manifest canonical JSON 各有 1 MiB 级硬上限，contract 诊断最多 64 条；当前不声称已有 history-list 边界。
- **NFR-B2B-004 — Honest UI**：状态文案使用“hard-cut 初剪”、“未建立批准”、“不可导出”、“素材绑定已变旧”；禁止“已定稿”、“完成剪辑”或“可发布”。
- **NFR-B2B-005 — Accessibility/responsive**：完整 materialize/inspect 流程可键盘操作，状态不只靠颜色，错误用 `aria-live`，支持 1440×1000、1280×800 和 390×844 且无页面级横向溢出。
- **NFR-B2B-006 — Performance**：256-Beat zero-gap fixture 的 materialize 本地 p95 目标为 500 ms 内（不含 preview render）；数据库验证不得依赖跨事务 trust cache 或 skip-validation flag。

## Out of scope

- Coverage Gap 填充、assignment 重选、global optimization 或局部 solver。
- 转场、字幕、旁白、任何音轨能力（source audio、配乐、混音、音量自动化）、速度变化、Technique/Craft compiler；v1 只保存 source audio disabled 的负能力。
- Canonical Timeline 手工 edit、restore/rebase/branch/undo/redo 及跨域 unified history。
- App preview、Timeline approval、render master、export/publish、usage ledger 与素材包；ML-017-A 可作为独立 native export consumer，但不扩大 B2B authority。
- 将 legacy Timeline 迁移或冒充 canonical current。
- 模型调用、新媒体分析、自由路径或用户原文件写入。

## Success criteria

- **SC-B2B-001**：1–256 Beat 的 zero-gap fixtures 中 Beat→clip 覆盖率 100%，重排/丢弃/额外 clip 为 0，compiler 不是 hard-cut/silent 或夹带 transition/audio-track 结构的误接受数为 0。
- **SC-B2B-002**：同一 exact project/upstream/evidence 输入在三次重放中 Timeline document/ID/content digest 一致率 100%，document 内 wall-clock/random/path/`asset_source_id` 字段为 0。
- **SC-B2B-003**：Gap、multi-selected、stale Blueprint/Coverage/evidence/source 和伪造 binding 矩阵中错误放行为 0，失败后 V7 部分写入为 0。
- **SC-B2B-004**：100 轮 empty-head first-cut 竞态中每项目 revision-1 winner 数为 1、既有 head 被 compiler 覆盖为 0，same-key replay 额外领域 mutation 为 0。
- **SC-B2B-005**：operation/revision/head/receipt/payload/source binding 各类 tamper 都返回 stable integrity error，legacy/current fallback 为 0。
- **SC-B2B-006**：workspace 中 draft 被 B2B 投影为 preview-ready/approved/exportable 的次数为 0；任何独立下游 adapter 若存在，必须重新验证 exact current revision/digest 和 current source。
- **SC-B2B-007**：V1–V6 migration/checksum exact-equal，current schema 的 fresh DB 包含 exact V7 Timeline catalogue，V6→current 升级通过 V7/V8 且保留可验证 pre-V7 backup，collision/recovery/tamper/rollback tests 通过，legacy Timeline regression 不退化。
- **SC-B2B-008**：focused contract/persistence/API/renderer-model tests 与一次 final-diff `npm run check` 通过后才能记为本地验证；真实 Electron materialize/inspect、未来 writable preview、Remote CI、tag/release 需分别取证，不得由本地 gate 替代。

## Promotion rule

当前 shared worktree 的 pure lowerer、V7 ledger、materialize/read API 与 workspace inspect 已在 [implementation-evidence.md](implementation-evidence.md) 记录 final-diff focused/full gate，并将验证状态更新为 `LOCAL VALIDATION WITH RESIDUALS`。SC-B2B-001–005 中尚未单独完成的更宽专项 campaign 仍按 evidence 标为 residual；initial Timeline→native export 已有真实 Electron package 证据，但 B2B2 V9 reconcile/inspection、Remote CI、tag/release 仍需分别取证。
