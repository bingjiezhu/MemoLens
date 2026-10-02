# Feature Specification: Canonical Timeline Reconciliation & Read-only Inspection

- Feature ID：`ML-015-B2B2`
- 创建日期：2026-08-23
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCAL VALIDATION WITH RESIDUALS`
- 父切片：[ML-015-B2B Deterministic Timeline Lowering](../015-b2b-deterministic-timeline-lowering/spec.md)
- 前置：[ML-018-A1 Canonical Coverage Plan](../../../018-script-coverage-global-footage-assignment/slices/018-a1-canonical-coverage-plan/spec.md)
- 实施证据：[implementation-evidence.md](implementation-evidence.md)

## Why this slice exists

B2B 已经能从空 head 生成 revision 1，但它故意拒绝任何已有 Timeline。Blueprint 或 Coverage 后续形成新 revision 时，旧 Timeline 只能显示 stale，不能安全前进；V7 operation/receipt 又把 command type 锁死为首次 materialize，不能靠改一处枚举冒充完整 successor history。

B2B2 只增加一条受限 successor 路径：

```text
exact stale canonical Timeline head N
  + exact current Blueprint
  + exact current、zero-gap、可重放的 A1 Coverage
  + transaction-local fixed source bindings
  → deterministic Timeline revision N+1
  → append-only parent/history/cold audit
  → renderer 中只读 inspection playback
```

它不是通用 NLE，也不允许 Coverage 覆盖手工 Timeline。当前系统尚无 canonical manual-edit command，因此 reconciliation 只接受由 B2B/B2B2 deterministic lowerer 形成的 lineage。

## Authority boundaries

- `timeline.reconcile_from_coverage/v1` 是 Desktop-authenticated、explicit project write；MCP 和普通 GET 保持只读。
- Core 在同一个 `BEGIN IMMEDIATE` 事务中重读 current Blueprint、Coverage 和 Timeline head，验证客户端提交的 exact preconditions，再执行 head CAS。
- 新 revision 必须以旧 head 为 parent，revision 必须精确为 `N+1`；旧 operation/revision/receipt 永不修改或删除。
- reconciliation 不检索、不重选素材、不填 Gap、不读取旧 Timeline 作为可自由编辑输入；它重新运行同一个 deterministic lowerer。
- inspection preview 只消费 normalized Timeline 与 exact source bindings。它没有 render/export/approval/Usage authority，`capabilities.preview` 仍为 `false`。

## User stories

### US1：上游变化后显式生成可审计 successor（P1）

用户刷新 Coverage 后，可以从一个明确 stale 的 Timeline head 生成 revision `N+1`，同时保留 revision `N`。

**Independent test**：先 materialize revision 1，再推进 Blueprint/Coverage，执行 reconcile；验证 parent、operation、receipt、head、upstream binding 和 source manifest 全部精确，same-key replay 为零额外写入。

### US2：并发、旧 head 和 current no-op 均失败关闭（P1）

**Independent test**：用 current head、旧 expected head、stale Coverage、带 Gap Coverage 和两个并发 reconciliation 请求分别执行；验证只有一个 winner，其他请求无部分 operation/revision/head/receipt。

### US3：升级不能破坏已有 Timeline 或 Export（P1）

V8→V9 migration 必须保持既有 revision/operation/receipt 原始 bytes/digests，并保留 Export/Usage ledger。

**Independent test**：迁移含 Timeline 与两个成功 Export 的 populated V8 数据库，比较迁移前后所有保留字段和 digest，运行 `foreign_key_check`。

### US4：用户可以播放检查，但不会被误导为最终预览（P1）

工作台按 exact hard-cut 顺序显示图片和 muted 视频 source span，支持播放、暂停和 scrub；binding 不匹配或媒体失败时整体/当前媒体失败关闭，不猜 fallback。

## Functional requirements

- **FR-B2B2-001**：V9 必须 additive 地扩展 canonical Timeline command vocabulary，同时保持 V1–V8 migration checksum 和既有 Timeline/Export digest bytes。
- **FR-B2B2-002**：若 SQLite 不能安全原地扩展 CHECK，migration 必须在 recoverable backup 后 same-name rebuild operations/receipts，并在失败时回滚。
- **FR-B2B2-003**：reconcile 输入必须是 closed exact `{database_uuid, blueprint_binding, coverage_binding, expected_timeline_head}`；未知字段拒绝。
- **FR-B2B2-004**：expected head 必须是当前 head、完整、revision/digest/Timeline ID exact-equal，且当前 Timeline 必须因 Blueprint/Coverage 变化而 stale；current no-op 返回稳定 conflict。
- **FR-B2B2-005**：current Coverage 必须仍是 zero-gap、exact A1 baseline derivation；Gap、stale evidence、manual/global 未支持来源均不得 lowering。
- **FR-B2B2-006**：输出 document 使用 revision `N+1` 和 exact parent binding；Timeline/track/clip deterministic identity 与首次 lowering 规则保持一致。
- **FR-B2B2-007**：operation、revision、receipt 只 append，head 只以 CAS 前进；same-key replay 返回冻结响应，same-key different request 冲突。
- **FR-B2B2-008**：cold audit 必须从 revision 1 顺序验证每个 historical Blueprint/Coverage derivation、parent、operation、receipt、source binding 和最终 head，不能只验证 latest row。
- **FR-B2B2-009**：read projection 必须区分 current、stale Blueprint、stale Coverage、stale evidence 和 stale source；Backend/Renderer reason vocabulary 必须一致。
- **FR-B2B2-010**：inspection preview 必须只采用同 backend origin 的 opaque media URLs；不得暴露本地 path、token、nonce 或 root identity。
- **FR-B2B2-011**：图片按 Timeline duration 显示；视频将 Timeline playhead 映射到 exact `[source_in_ms,source_out_ms)`，始终 muted 且不越过半开右边界。
- **FR-B2B2-012**：project/DB/revision/digest identity 变化、unmount 或 source error 必须停止播放并清理 animation frame；不得继续播放旧 project。
- **FR-B2B2-013**：inspection preview 不写 Timeline、不生成 render job、不批准 export、不产生 Usage，也不把 `capabilities.preview` 改为 true。

## Out of scope

- replace、trim、reorder、subtitle、audio level、undo/redo、branch/restore 或通用 manual Timeline edit。
- 从 manual current 自动 rebase/reconcile，或让旧 Coverage 覆盖用户编辑。
- final-fidelity render preview、audio、transition、caption、color/crop fidelity。
- Global Assignment、局部影响集重算、B2C unified history、Remote CI 和 release。

## Promotion rule

本切片只有在 V8→V9 populated migration、reconcile concurrency/idempotency/tamper、Backend/Renderer reason parity、inspection model/build 和整仓回归通过后，才可写成 `LOCAL VALIDATION WITH RESIDUALS`。没有真实 Electron reconcile/inspection、manual edit 与 final-fidelity preview 证据，因此不能写成 V3 complete。
