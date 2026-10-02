# Feature Specification: Canonical Timeline History + Append-only Restore Foundation

- Feature ID：`ML-015-B2C0`
- 创建日期：2026-08-23
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCAL REPOSITORY GATES PASSED; REAL-HOST JOURNEYS PENDING`
- 父规范：[ML-015 Agent-Agnostic Creative Protocol](../../spec.md)
- 前置切片：[ML-015-B2B4 Codex / DeepSeek Canonical Editor Handoff](../015-b2b4-codex-deepseek-canonical-editor-handoff/spec.md)

## Why this slice exists

B2B3/B2B4 已把一组 closed edit 表达为 append-only canonical Timeline revision，但用户还不能在同一 Timeline 中检查旧 revision 并把某个历史内容明确保存为新 head。B2C0 只交付这个最小闭环：

```text
current Timeline head N
  + read-only historical revision K
  + explicit Save K as N+1
  + exact CAS on N and current Blueprint/Coverage bindings
  → append restore operation
  → revision N+1, parent=N, restore_from=K
  → canonical reread
```

它不建第二份 history store。历史仍是 `canonical_timeline_operations + revisions + head + receipts`；Codex、DeepSeek 与 Desktop 只读写同一 `database_uuid + project_id + canonical head`。

## Product boundary

### History selection is not write authority

- current head workspace 继续是 edit、restore CAS 和 Export 的唯一权威状态。
- historical selection 只是独立的 read-only projection，不得覆盖 current workspace。
- 历史 K 不会因为被选中而变成 head；只有显式 Save 才能追加 N+1。

### Restore is an append-only command

`timeline.restore_revision/v1` 必须满足：

- `K < N`，且 K/N 属于同一 project 与 Timeline。
- K、N 与 current Blueprint/Coverage 的 exact bindings 完全相同。
- K 和 N 的 fixed source bindings 在事务内仍可验。
- N+1 的 `parent=N`；K 只记录在 `restore_from`，不伪装成 parent。
- K、N 与所有旧 operation/revision/receipt 字节不改写。

Timeline JSON 的 `timeline_content_sha256` 包含 `revision` 和 `parent`，因此“从 K 精确恢复内容”不等于 N+1 整份 JSON/digest 与 K 相同。冻结语义是：

1. 只将 K 的 `/revision` 重包为 N+1；
2. 只将 K 的 `/parent` 重包为 N；
3. 其余 canonical Timeline payload 的规范值与 K 相同；
4. `source_bindings_json` 规范字节与 digest 与 K 相同。

### Restore requires a separate capability

`timeline.restore_revision` 是新的 project-bound action，不由 `timeline.apply_edit` 隐式继承。由于 V12 已冻结三 action closed union，B2C0 必须使用显式 V13 migration 扩展 capability/event CHECK 约束，不得原地改 V12 checksum。

V13 必须保留旧 capability facts、receipt bytes、event SHA 和 parent-event SHA。新 restore write 使用 V12 已统一的 generic exact-request v2 receipt lane，不新建第三张 receipt 表。

## User stories

### US1：检查 Timeline 历史（P1）

**Given** current head 为 N，**When** 用户选择历史 K，**Then** 页面以只读方式显示 K，current head 和 Export authority 仍为 N。

### US2：显式保存 K 为 N+1（P1）

**Given** K/N 与 current upstream bindings 一致，**When** 用户点击 `Save revision K as N+1`，**Then** Core 追加 restore operation 和 N+1，然后页面以 canonical reread 确认结果。

### US3：并发与 stale 不静默覆盖（P1）

**Given** 用户从 N 准备 restore，**When** 另一 edit 先创建 N+1，**Then** restore 以 `timeline_head_conflict` 失败，不产生部分行或自动 rebase。

### US4：Codex/DeepSeek 只能用显式 restore 授权（P1）

**Given** capability 只有 `timeline.apply_edit`，**When** Canonical Editor 尝试 restore，**Then** 在任何 ledger mutation 前拒绝；拥有 exact restore action 的 capability 才可通过同一 nonce/HMAC/CAS 路径保存。

## Functional requirements

- **FR-B2C0-001**：历史读取复用 canonical Timeline revision ledger，不新建 host-local history。
- **FR-B2C0-002**：historical selection 与 current writable workspace 必须分离。
- **FR-B2C0-003**：restore 请求必须绑定 exact DB/project/N/K/Blueprint/Coverage/source facts。
- **FR-B2C0-004**：Core 必须在同一 verified transaction 重读 N、K 和 upstream，然后追加 operation/revision/head/receipt。
- **FR-B2C0-005**：N+1 只重包 K 的 revision/parent，其余 Timeline payload 与 source bindings 必须 exact replay。
- **FR-B2C0-006**：`timeline.restore_revision` 是独立 capability action；edit-only capability 必须拒绝。
- **FR-B2C0-007**：V13 只扩展 frozen action/event schema，不改 V12 migration row/checksum 或旧 digest。
- **FR-B2C0-008**：Desktop 与 paired restore 使用同一 Core contract，但各自保留 desktop/paired provenance 与权限边界。
- **FR-B2C0-009**：同 key/同 request replay 返回冻结结果；同 key/不同 request conflict；不重复消耗 capability。
- **FR-B2C0-010**：成功后必须 project refresh + exact Timeline reread，不用乐观本地状态合成 N+1。
- **FR-B2C0-011**：冲突、upstream/source stale、授权失效和 post-commit 不确定均必须有明确恢复状态。
- **FR-B2C0-012**：页面与文档不得把本切片命名为完整 Undo/Redo/Branch。

## Stable errors

| Code | Meaning |
| --- | --- |
| `timeline_restore_target_invalid` | K 不存在、不属于该 Timeline 或 K≥N |
| `timeline_restore_binding_mismatch` | K/N/current Blueprint/Coverage 不是同一 binding |
| `timeline_restore_source_stale` | K 或 N 的 fixed source facts 不再可验 |
| `timeline_head_conflict` | expected N 不再是 current head |
| `timeline_idempotency_conflict` | 同幂等 identity 绑定不同 restore request |
| `agent_pairing_scope_denied` | capability 不包含 `timeline.restore_revision` |
| `canonical_editor_post_commit_reconciliation_required` | commit 可能成功，但 canonical reread 未确认 |

## Required test matrix

| Lane | Required falsification |
| --- | --- |
| Pure contract | exact re-envelope；K≥N；payload/source-binding drift；deterministic replay |
| V13 migration | populated V12→V13；backup；copy/swap fault；name collision；future schema；旧 receipt/event/digest preservation |
| Transaction | K→N+1；CAS race单赢家；replay/conflict；fault rollback；cold reopen |
| Authority | edit-only deny；restore-only allow；path/body/action/nonce substitution；exhausted replay |
| History/UI | historical selection 不替换 current authority；pending 非 canonical；Save then reread |
| Recovery | pre-commit crash；post-commit/reread uncertainty；conflict refresh success/failure |
| Host parity | Codex/DeepSeek/Desktop 读到同一 N+1；不传聊天或 host-local Timeline |
| Repository gate | Core/plugin V13 parity；Python/plugin/Node/renderer/typecheck/build/diff |

## Out of scope

- Blueprint/Coverage/Timeline 跨层全局 operation order。
- 通用 undo/redo stack、branch graph 或 merge。
- 跨 Blueprint/Coverage binding 的 Timeline restore。
- split/delete/transition/crop/keyframe/multi-track 等新 edit dialect。
- 字幕、封面、音轨、final-fidelity preview/render 和完整素材包。

## Current implementation boundary

当前 shared worktree 已实现 V13 migration、Core restore contract/cold audit、Desktop 与 paired API、React workspace、Codex/DeepSeek 共用 Canonical Editor，以及 read-only K 与 current N 分离的可见 UI。历史检查不授予写权限；`timeline.restore_revision` 只控制 stage/save。真实 Codex 与 DeepSeek 模型会话的双向 host/UI journey、Remote CI、commit/tag/release 和完整 B2C cross-resource history 仍未完成，不能据此宣称 B2C 或 V3 完成。精确证据见 [implementation-evidence.md](implementation-evidence.md)。
