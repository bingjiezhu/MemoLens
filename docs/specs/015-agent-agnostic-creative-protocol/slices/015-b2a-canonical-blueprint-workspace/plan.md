# Implementation Plan: ML-015-B2A Canonical Blueprint Workspace & Honest History Bridge

- 状态：`IMPLEMENTED / EVIDENCE CLOSURE IN PROGRESS`
- 实施状态：`IMPLEMENTED`
- 验证状态：`LOCAL VALIDATION WITH RESIDUALS`
- 原则：先冻结真源/权威/可执行/历史边界，再用 failing tests 锁定 409 断点；仅做 V5 上的 read/UI/restore bridge，不提前实现 compiler 或 unified ledger。
- 规范：[spec.md](spec.md)
- 证据：[implementation-evidence.md](implementation-evidence.md)

## 1. Architecture

```text
Renderer project resume
  → project_id is a hint, never current state
  → existing authenticated Core project read
  → one repository read boundary
       bind exact database_uuid
       validate exact project + explicit Blueprint head
       validate current Blueprint revision/operation/receipt/digests
       validate decision-authority projection as of exact revision
       project honest executable state: not_compiled
       project three bounded history lanes
       project server-derived capabilities
  → canonical workspace renderer model
       Blueprint sections
       authority units
       honest history lanes
       disabled Timeline/Preview/Render surface

History revision selected
  → read exact validated target
  → current-to-target 12-section compare
  → user reviews explicit new-revision effect
  → existing desktop-authenticated restore command
       expected_head = exact current
       restore_from = exact target
       durable idempotency receipt
       expected_database_uuid + expected_head checked in the write transaction
  → refetch whole workspace from Core
```

### Responsibility boundaries

| Layer | Owns | Must not own |
| --- | --- | --- |
| Core repository + `backend/src/media/blueprint.py` | V5 canonical validation, snapshot-consistent reads, exact database/project/Blueprint/authority/history metadata, existing CAS restore | presentation wording, renderer cache truth, compiler inference |
| Backend media/Blueprint service | safe workspace/history envelope, stable errors, bounded/redacted projections, server-derived capabilities | raw SQL/path/secret exposure, second mutation path |
| Existing restore command | exact expected head, exact target, durable replay, append-only revision/operation result | UI diff claims, automatic conflict retry, authority copying |
| Renderer API normalizers | closed typed normalization, unknown/missing field rejection or honest downgrade | accepting legacy brief as canonical, fabricating executable state |
| Workbench reducer/model | one workspace state machine, loading/stale/conflict/failure state, selected revision and compare presentation | persisted project truth, semantic authority, local optimistic head mutation |
| Workbench UI | progressive Blueprint disclosure, authority/executable labels, three lanes, compare/restore review, responsive/a11y behavior | confirm/revoke authority writes, legacy Timeline execution, unified-ledger claims |

## 2. No-Migration Strategy

B2A 使用现有 V5 数据：

- `creative_projects` / `creative_briefs` / `timelines` 仅提供项目 identity 和 legacy metadata。
- `creative_blueprint_heads` / `creative_blueprint_revisions` / `creative_blueprint_operations` / receipts 提供 semantic lane。
- `blueprint_decision_authority_*` 提供 current authority projection 和安全事件 lane。
- 现有 Timeline revision 仅作 `historical_observed_context_only` metadata；brief 与 lane 为 `migration_context_only`，均不解析为 current executable。
- legacy JSON 在 canonical decode 前执行每值 1 MiB、聚合 16 MiB byte preflight；brief 与 Timeline 共用一个 `history_limit`，不伪造跨类型 total order。

实施前后必须比较：

1. V1–V5 migration tuple/checksum exact equality。
2. B1A managed V5 normalized `sqlite_schema` allowlist exact equality。
3. fresh database 和 v4→v5 fixture 的 schema version 仍为 5。
4. 新 table/index/trigger/view 数为 0。

任何实现若需要 persistent cursor、checkpoint、cross-domain binding 或 Timeline compiler manifest，必须停止并转为独立 V6/B2B Spec，不在 B2A 里偷渡。

## 3. Phases

### Phase 0 — Baseline lock and red tests

1. 记录 branch/HEAD/package version/现有 dirty state，不覆盖其他已有改动。
2. 冻结 V5 migration/checksum/schema allowlist snapshot 和 healthy/corrupt Blueprint fixtures。
3. 运行现有 B0/B1/B1A、legacy Timeline 和 renderer-model baseline；若 baseline 本身失败，先分类是环境、既有 dirty diff 还是本切片。
4. 先新增 failing tests：healthy Blueprint project 当前返回 409；工作台不能 restore current Blueprint；缺少诚实 executable/history projection。

**Gate P0**：记录可重现的 red failure，并证明 legacy-only project 当前可正常打开。

### Phase 1 — Pure workspace and history contracts

1. 定义 canonical workspace、executable state、capability projection 和 3-lane history 的 backend/renderer typed model。
2. 定义 safe field allowlists；特别锁死 authority lane 仅有 `event_id/sequence/authority_operation/observed_revision/decision_units/created_at`。
3. 定义 12-section history diff 与 current→selected compare 的不同语义。
4. 定义 workspace reducer 状态：`idle/loading/ready/reloading/conflict/integrity_error/unavailable`，并把 selected target 与 canonical head 分开。
5. 在任何 UI 组件前用纯模型测试覆盖缺字段、未知枚举、secret 字段、stale head 与 legacy fallback。

**Gate P1**：输入同一 fixture 时 backend/renderer 对 semantic/authority/executable/history 的投影一致；未知或矛盾能力 fail closed。

### Phase 2 — Snapshot-consistent Core/backend read bridge

1. 在同一 repository transaction/read boundary 内读取 exact `database_uuid`、project identity、explicit Blueprint head、exact current revision、authority projection 和三条 latest bounded window。
2. 复用现有 full/verified-prefix validators，不新增 skip-validation 参数或 UI-controlled trust flag。
3. 将 healthy Blueprint project 的 project read 从 409 替换为 workspace 200；保留 corrupt-head stable error 与 legacy-only response。
4. 实现 latest bounded history windows，仅返回安全 metadata、available totals 与 truncation；B2A 不提供 cursor/continuation。semantic window 以 operation total 锚定并按 sequence 连续降序；authority 选最新窗口后按 sequence 连续升序；legacy 使用精确 order literal `brief_revision_desc_and_timeline_created_at_desc_id_asc_revision_desc` 和 brief/Timeline 共享预算。
5. 在服务端导出 executable/capability projection；对 Blueprint project 仍保留 create Timeline、revise Timeline 和 new render 的硬阻断。
6. 对 semantic/authority canonical 损坏整体 fail closed；对独立 legacy metadata lane 损坏只投影 lane unavailable，不伪造 current。

**Gate P2**：API focused tests 覆盖 healthy/current/exact/corrupt/concurrent/latest-window/privacy，且 schema diff 为空。

### Phase 3 — Renderer state and project resume

1. 扩展 TypeScript types/normalizers，拒绝把 Blueprint workspace 规范化成 legacy `brief` current。
2. 重构 workbench restore：`localStorage` 仅提供 project ID，整个 current state 从 Core 响应建立。
3. 使用一个 reducer 管理 workspace current、selected historical revision、compare 和 conflict；不在成功 restore 后本地直接增加 revision。
4. 连接既有 exact Blueprint read/history/restore command，只在 server-derived capability 允许且用户完成审阅时提交。
5. 对 abort/unmount/runtime switch/head conflict 取消旧 request 或忽略过期 response；child 与 parent 都校验 captured/current scope、database UUID、project 与 head identity，防止旧 workspace 覆盖新 database/project。

**Gate P3**：reducer/model tests 证明 stale response 不能成为 current，CAS conflict 后 mutation count=0，success 后仅 Core refetch 可推进 current。

### Phase 4 — Workbench UX

1. 在当前 MemoLens 视觉语言上增加 Blueprint summary/section cards，不全屏显示 raw JSON。
2. 增加独立的 proposal、decision authority 和 executable 标识；使用诚实用语和解释。
3. 增加 3-lane history 和 section compare；authority/legacy lane 不出现不支持的 restore/execute 动作。
4. 增加 restore review/conflict/reload 状态，并保留随时取消；Desktop HTTP 强制传入 `expected_database_uuid`，command identity 由 database/head/target 稳定决定。
5. 用 honest empty state 取代 Blueprint project 的 legacy Timeline canvas，明确 compiler 尚未实现。
6. 完成桌面与 390px 响应式、键盘、focus、ARIA 和触摸目标验收。普通 browser 只验证只读 workspace/compare/layout；authenticated writable restore/CAS conflict 必须在真实 Electron Desktop 独立验收。

**Gate P4**：真实 renderer/browser 截图覆盖规范所列关键状态，console error/broken image/page overflow 均为 0；截图归档与真实 Desktop writable flow 不得由只读 browser smoke 替代。

### Phase 5 — Regression, adversarial review, and evidence

1. 重跑 B0/B1/B1A Blueprint/authority/integrity tests，确保 read bridge 没有放松 ledger 验证。
2. 重跑 legacy Timeline create/revise/render guard tests，并验证 legacy-only project 不退化。
3. 重跑 plugin safe readers/MCP capability，确保 `write=false` 与隐私边界不变。
4. 运行 history 有界性/隐私矩阵、restore 100-round race 和 focused local benchmark。
5. 在当前 diff 上运行完整 `npm run check`，再由独立审查者检查真源、authority、stale response、legacy fallback 和 schema 边界。
6. 只将实际观察到的命令/计数/截图/结论写入 evidence；然后再更新 Spec/Tasks 状态。

**Gate P5**：无未解决 P0/P1；所有声称均与证据和 Remote CI 实际状态一致。

## 4. Test Matrix

| Domain | Positive oracle | Failure/adversarial oracle |
| --- | --- | --- |
| Canonical read | healthy head returns exact workspace 200 | missing/corrupt head, revision, operation, receipt, digest, schema never falls back |
| Authority | 0/8, partial, 8/8, post-edit invalidation parity | corrupted event/receipt fails closed; renderer cannot fabricate/confirm |
| Executable state | all B2A Blueprint projects report `not_compiled` | legacy Timeline cannot become current/create/revise/render |
| Semantic history | latest bounded sequence/parent/changed sections/result，窗口从 total 锚定 | out-of-order/non-contiguous/tampered/truncated data rejected |
| Authority history | latest bounded `event_id/sequence/authority_operation/observed_revision/decision_units/created_at` | non-contiguous latest window 与 nonce/token/path/epoch/raw presentation leak checks |
| Legacy history | shared-budget brief/Timeline metadata with exact roles/order | predecode byte exhaustion、raw JSON/locator/path absent；corrupt lane never selected current |
| Compare | exact current→target 12-section parity | parent→result diff not reused as compare |
| Restore | exact database UUID + head CAS creates N+1 and refetches | database switch、concurrent head conflict、target digest mismatch、lost-response replay |
| Resume | project ID refetches canonical Core state | stale/local cached response cannot win after runtime/database switch |
| Responsive UI | 1440×1000, 1280×800, 390×844 | overflow, clipped CTA, sub-44px touch, color-only state, focus loss |
| Regression | legacy-only project and B0/B1/B1A/plugin remain green | V5 schema/checksum drift, new MCP write, file/network/model side effect |

## 5. Rollout and Rollback

- 这是 application/API behavior 变更，不是 data migration；打开 workspace 不会写数据。
- Restore 仍是用户明确触发的可恢复 project mutation，已有 durable receipt/CAS 语义不变。
- 若 workspace projection 出现未解决的完整性或误导风险，回滚为诚实的 Blueprint-workspace unavailable read 错误；不删除、重写或降级任何 Blueprint/authority 数据。
- 不做 destructive down-migration，因为 B2A 不应有 migration。若 implementation diff 出现 schema 对象或 checksum 变化，release gate 必须直接失败。
- 旧版 renderer 仍可能将 Blueprint workspace object 视为不支持响应；发布说明必须冻结 backend/renderer 同版本升级边界，不声称任意交叉版本兼容。

## 6. Current Closure State

Phases 1–4 的实现已存在；focused API、renderer/session、typecheck、100-round race 与只读 browser smoke 已运行。交易内 typed ledger attestation 将 100+100+100 warm profile p95 从 `1760.719 ms` 降至 `227.083 ms`，已通过 500 ms 目标；证明同时绑定 main-schema 名称解析环境。final-diff `npm run check` 已通过。当前仍不能关闭 P4，且 P5 受 P4/历史证据前置约束：1280×800 未真实验收，browser 截图未保存为 repo-local artifact，完整键盘/focus 与真实 Electron Desktop writable restore/CAS conflict 视觉验收未完成；实施前 v4→v5 snapshot/baseline 与 red-phase transcript 没有保留，Remote CI 尚未运行。实施详情与精确证据边界见 [implementation-evidence.md](implementation-evidence.md)。

## 7. Completion Rule

只有 [tasks.md](tasks.md) 中所有 P0 task 完成、[evidence](implementation-evidence.md) 已记录当前 diff 的实际结果，且独立复审无未解决 P0/P1，才能将本计划改为 `COMPLETE / LOCALLY VALIDATED`。当前状态说明实现、自动化本地 gate、局部正确性与性能验证成立；未完成的历史 baseline/red 证据、视觉/Desktop 验收与 Remote gate 必须继续显式保留。
