# Feature Specification: Canonical Usage Projection & Brief Admission

- Feature ID：`ML-017-A2`
- 创建日期：2026-08-23
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCAL VALIDATION WITH RESIDUALS`
- 前置：[ML-017-A Canonical Export & Success-only Usage](../017-a-canonical-export-usage-ledger/spec.md)
- 实施证据：[implementation-evidence.md](implementation-evidence.md)

## Why this slice exists

ML-017-A 已把成功导出的每个 clip 固定为 immutable Usage occurrence，但“素材是否已用、哪一段仍可用”还没有安全的读模型。若检索只查 occurrence 存不存在，删除/篡改会伪造 unused；若 UI 只传 match ID，搜索后的新 Export 会让 no-reuse 约束在创建 brief 时失效。

A2 保持单一事实源：

```text
validated successful Usage occurrences
  + current asset source domain
  → deterministic used union / residual projection
  → unused / prefer-unused / allow-reuse search explanation
  → exact usage_selection
  → same-transaction Director admission
```

它不新增可写 `used` flag，也不把 residual interval 伪装成已有 segment identity。

## Authority boundaries

- Usage occurrence 仍只由 successful canonical Export Revision 原子产生；preview、失败、取消和 interrupted 不产生事实。
- Search projection 是可重建 read model。absence 声明前必须 cold-audit 所有仍被五张 Export 表任一关系引用的 project。
- Renderer 必须完整验证 `usage_revision` 和每个 11 字段 Usage projection，但 Renderer 不是最终 authority。
- Create Brief 必须提交 closed `usage_selection`；Director 在同一个 `BEGIN IMMEDIATE` transaction 内重读 current candidates、validated Usage、revision 和 exact projection后再写 project/brief。
- `unused_only` 的 admission 单位当前是完整 analyzed segment：只要该 candidate window 有任一 used interval 就拒绝。UI 可以显示 residual，但不能提交整段复用。

## User stories

### US1：用户可以看到成功导出后的精确剩余区间（P1）

**Independent test**：对重叠、相邻和重复 half-open occurrences 计算 union/residual，并用 750 组随机 fixtures 与逐毫秒 oracle 比较。

### US2：unused/prefer-unused 不因坏数据而失败开放（P1）

**Independent test**：删除 occurrence，或同时删除 operation+target occurrence 并保留 job/revision/receipt；search 必须抛 integrity error，不返回 false unused。

### US3：搜索到创建 brief 之间的并发 Export 不会绕过 no-reuse（P1）

**Independent test**：先生成 usage-aware search selection，再新增 successful Usage 或改变 candidate analysis head/domain；create brief 返回 409，project/brief/idempotency partial writes 为 0。

### US4：导出结果自动收敛，不要求用户猜何时 Refresh（P1）

workspace 在 native command submitted/unknown 后只轮询 canonical project read，绑定 exact job/identity/head，终态后停止；manual Refresh 仍保留。

## Functional requirements

- **FR-A2-001**：video Usage 使用整数 half-open `[start_ms,end_ms)`；union 必须合并 overlap 与 adjacency，residual 必须与 used 不重叠且精确覆盖 source/candidate domain。
- **FR-A2-002**：image 使用 occurrence 语义，不伪造时间区间；`source_domain/candidate_domain=null`，used/residual arrays 为空。
- **FR-A2-003**：asset source duration 必须为正整数；occurrence 和 candidate 必须落在 `[0,duration_ms)` 内。同一 asset 的 candidate duration 不一致时失败关闭。
- **FR-A2-004**：video analysis writer 必须在写 segment/transcript/keyframe 前验证其范围不越过已 probe 的 asset duration；keyframe timestamp 不得等于半开右边界。
- **FR-A2-005**：Usage absence 前必须从 Export operations/jobs/revisions/occurrences/receipts 的 project ID UNION 枚举并 cold-audit；任一残留 orphan evidence 都必须暴露篡改。
- **FR-A2-006**：search 支持 closed `unused_only`、`prefer_unused`、`allow_reuse`、`used_in` 和 `residual_of` 组合；冲突组合拒绝。
- **FR-A2-007**：usage-aware response 必须携 lowercase 64hex `usage_revision`；每个 match 必须携 closed 11 字段 projection，缺失、额外字段或不变量不成立时整个响应拒绝。
- **FR-A2-008**：`prefer_unused` 以 exact candidate window 而不是整文件判定 fresh；另一窗口已用不应污染完全未覆盖的 segment。
- **FR-A2-009**：Create Brief 的 `usage_selection` 必须闭合为 policy、revision、非空 candidates；candidate refs 唯一且与 9 字段 candidate identities 同序一一对应。
- **FR-A2-010**：Director admission 必须与 project/brief/idempotency 写入共享同一个 immediate transaction，重算所有 current candidate Usage 和 revision；stale revision/identity/domain/projection 返回 409。
- **FR-A2-011**：`unused_only` 拒绝 used image 和任何含 used interval 的完整 video candidate；`prefer_unused/allow_reuse` 保存完整 selection，并在 provenance 固定 policy/revision/projection digest。
- **FR-A2-012**：切换 policy 或发起新 search 必须清空旧选择；usage-aware 模式无当前显式选择不得回落到无 policy 自动检索。
- **FR-A2-013**：export polling 必须 bounded、可取消，只读 canonical workspace，不重发 export/capability；project/DB/head 切换、unmount 或 terminal 后停止。

## Out of scope

- 为 residual interval 创建新的稳定 clip/span identity；因此 partial-used video 当前只能解释，不能在 `unused_only` 下选入。
- derivative-output/final-video exclusion、Usage correction/supersession 和 final/test role 修正。
- materialized Wiki、完整 ML-017-B source package、发布/上传/移动原件。
- 真实 Electron 自动 polling 视觉验收、Remote CI、release。

## Promotion rule

只有 interval oracle、cold-audit tamper、source bounds、strict Renderer adoption、same-transaction brief conflict/rollback、polling model 和整仓回归通过，才可记为 local validation。未交付 residual identity、derivative exclusion 和 fresh Electron polling，因此不能声称完整 V4 或完整 parent ML-017。
