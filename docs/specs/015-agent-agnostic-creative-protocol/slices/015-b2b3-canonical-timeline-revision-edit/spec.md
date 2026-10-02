# Feature Specification: Canonical Timeline Revision Edit

- Feature ID：`ML-015-B2B3`
- 创建日期：2026-08-23
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCAL VALIDATION WITH RESIDUALS`
- 父切片：[ML-015-B2B Deterministic Timeline Lowering](../015-b2b-deterministic-timeline-lowering/spec.md)
- 前置：[ML-015-B2B2 Timeline Reconciliation & Inspection](../015-b2b2-timeline-reconciliation-inspection/spec.md)
- 产品决策：[43 问第 25–30、32–34 项](../../../product-decisions-43-questions-2026-08-22.md)
- 实施证据：[implementation-evidence.md](implementation-evidence.md)

## Why this slice exists

B2B/B2B2 已能把 exact Blueprint + Coverage 降低为可检查、可导出的 canonical Timeline，但用户只能查看机器生成的 silent hard-cut head。Legacy Workbench 可以剪却不属于 canonical 项目；Codex/DeepSeek handoff 编辑器有按钮却只保存进程内草稿。继续扩展任一旧路径都会形成第二份 Timeline 真源。

B2B3 建立第一条受控的手工剪辑链：

```text
exact current canonical Timeline N
  + exact pinned Blueprint/Coverage/source facts
  + one closed user edit
  → deterministic preview
  → Desktop-authenticated append-only command
  → Timeline revision N+1
  → reopen / inspection / canonical export all observe N+1
```

本切片首先完成常用基础视觉操作，不加入复杂特效、音频、字幕、branch 或 Agent 写权限。Codex/DeepSeek 的统一 canonical editor handoff 由后续 B2B4 消费同一命令；当前 process-scoped Timeline 1.0 editor 不得被描述为已保存。

## Authority boundaries

- `timeline.apply_edit/v1` 是 reversible project write，只接受 Desktop-authenticated POST；GET/MCP 保持只读。
- Core 在一个 `BEGIN IMMEDIATE` 事务内重读 database、project、Blueprint、Coverage、Timeline head 与 source identity，再执行 append/head CAS/receipt。
- 客户端只提交一个 closed edit 和 exact preconditions；不得提交 path、source locator、actor、authority、result Timeline 或 replacement source identity。
- replace 只能引用当前 pinned Coverage 中同一 Beat 的 verified alternative assignment；Core 自己解析 source。
- edit 不修改 Blueprint、Coverage、原媒体或历史 revision。成功 export 仍需独立 native approval。
- Timeline 文档现有 compiler capability 继续表示 hard-cut/silent dialect；手工来源由 canonical command ledger 证明，不能再以 exact Coverage-lowering derivation审计。

## User stories

### US1：视频片段可以裁剪并保存为新 revision（P1）

**Given** current Timeline 中一个 video clip 绑定了 pinned Coverage span，**When** 用户在工作台调整 source in/out 并保存，**Then** 新区间必须位于 proof bounds、时长为正，Timeline 后续片段确定性回流，revision 追加为 `N+1`。

**Independent test**：保存后刷新和重新打开项目仍显示 `N+1` 与新 source interval；旧 revision N bytes 不变；canonical export presentation 绑定 `N+1` digest。

### US2：图片时长与镜头顺序可以编辑（P1）

**Given** hard-cut Timeline 有 image/video clips，**When** 用户调整图片时长或把一个 clip 移到同轨其他位置，**Then** ordinal、连续 start/end 和 output duration 由 Core 重算，不能由客户端伪造。

### US3：同一 Beat 可以换成 Coverage 已证明的备选素材（P1）

**Given** Beat 有一个 verified、足够长且未被其他 clip 占用的 alternative assignment，**When** 用户选择 Replace，**Then** Core 解析 exact asset/span/source，生成新 clip/source binding；unresolved、过短、跨 Beat、重复 evidence 或 stale source 全部拒绝。

### US4：并发、重放和篡改不会静默覆盖（P1）

两个使用同一 expected head 的 edit 只能有一个成功；相同 idempotency key + 相同请求返回冻结 receipt，key 与不同请求冲突。冷启动必须从 revision 1 依次重放 lowering/reconcile/edit，任一历史 edit/result/source tamper 都使项目 fail closed。

## Functional requirements

- **FR-B2B3-001**：新增 closed `timeline.apply_edit/v1`；一次命令只包含一个 edit。
- **FR-B2B3-002**：edit v1 只允许 `trim_clip`、`set_clip_duration`、`move_clip`、`replace_clip`；未知字段和 bool-as-int 拒绝。
- **FR-B2B3-003**：trim 只适用于 video，source interval 必须在 pinned Coverage proof 内且至少 100 ms；source/timeline duration 必须相等。
- **FR-B2B3-004**：set duration 只适用于 image，必须为 bounded positive integer。
- **FR-B2B3-005**：move 只在唯一 primary visual track 内执行；目标 index 必须在当前 clip 集合内。
- **FR-B2B3-006**：replace 只采用同 Beat current Coverage alternative；proof 必须 verified、时长满足原 slot、source current，且 evidence 不得与其他 clip 重复。
- **FR-B2B3-007**：所有 edit 都由 Core 重排 ordinal/start/end/output duration，并生成 exact parent `{revision,content_sha256}`；不得接受 caller-supplied result document。
- **FR-B2B3-008**：Blueprint/Coverage binding、aspect ratio、hard cut、cover fit、silent/no-subtitle capability保持不变。
- **FR-B2B3-009**：V10 migration 不重写 V1–V9 history bytes/digests；只扩展已泛化 command vocabulary 与 migration record。
- **FR-B2B3-010**：operation result 必须保存 closed edit，cold audit 从 parent replay 并比较 exact Timeline/source manifest。
- **FR-B2B3-011**：head CAS、receipt replay/conflict、verified-prefix delta、resource budgets 与 fault rollback必须与 B2B2 同级。
- **FR-B2B3-012**：React canonical workspace 提供明确的 Edit controls、pending preview、Discard 和 Save as revision N+1；不得自动保存。
- **FR-B2B3-013**：保存成功后 UI 重新读取 canonical resource；不能用乐观 local state冒充持久 head。
- **FR-B2B3-014**：stale Blueprint/Coverage/source、archived project、non-head revision 或未知 DB identity 不得编辑。
- **FR-B2B3-015**：inspection preview 可以显示 pending edit，但 render/export/Usage 只消费持久 canonical head。

## Out of scope

- split/delete、多轨、transition、crop/keyframe、subtitle、source audio、配乐、旁白、混音与 final-fidelity render preview。
- undo/redo/restore/branch、manual-current Coverage rebase 与 B2C unified cross-domain history。
- Codex/DeepSeek paired write、canonical project deep-link/handoff 与把 Timeline 1.0 草稿迁入 canonical ledger；这些进入 B2B4。
- Global Assignment、Technique compiler、Remote CI、签名、公证与 release。

## Promotion rule

只有 pure edit/replay、V9→V10 populated migration、ledger cold audit、service/API concurrency/idempotency/fault、Renderer normalizer/UI model 与整仓回归通过，且至少完成一次真实“edit → save N+1 → reopen → inspection”旅程，才可写成 `LOCAL VALIDATION WITH RESIDUALS`。没有 B2B4 host handoff、final-fidelity preview、audio/subtitle、B2C 和 fresh export journey时，V3 仍不能标完成。
