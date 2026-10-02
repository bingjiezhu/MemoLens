# Feature Specification: Canonical Coverage Plan Baseline

- Feature ID：`ML-018-A1`
- 创建日期：2026-08-23
- 实施状态：`IMPLEMENTED`
- 验证状态：`LOCAL VALIDATION WITH RESIDUALS`
- 授权依据：用户于 2026-08-23 授权按产品目标调整并持续实现全部必要切片
- 父规范：[ML-018](../../spec.md)
- 前置：[ML-015-B0/B1/B2A](../../../015-agent-agnostic-creative-protocol/spec.md)、当前 canonical media evidence
- 后续：[ML-015-B2B deterministic Timeline lowering](../../../015-agent-agnostic-creative-protocol/spec.md)；[ML-018-A0 Audio Timing Evidence](../../spec.md) 是后续 timing enrichment，不是 A1 baseline 的前置
- 实施证据：[implementation-evidence.md](implementation-evidence.md)

## Why this slice exists

MemoLens 当前有 canonical Creative Blueprint，也有 legacy Timeline，但没有一个单独表达“这段文稿需要什么画面、实际选了哪段私人素材、哪里仍缺”的项目状态。若直接让 Timeline compiler 从自由 Blueprint 再次选材，素材规划会隐藏在编译器中，并与 ML-018 的全局分配形成第二个 planner。

本切片建立唯一的中间真源：

```text
Creative Blueprint（表达与约束）
  → Coverage Plan（Beat / Need / Assignment / Gap）
  → Timeline（可执行剪辑）
```

首版是诚实 baseline：它只把 Blueprint 已固定的 script blocks 与 `material_hints` 规范化为计划，不声称完成语义理解、气口判断或全局最优分配。没有 evidence 的 Beat 保留 Gap，不用无关素材填满。

## User stories

### US1：每段脚本都有稳定、可追踪的覆盖状态（P1）

用户打开项目时，可以看到每个 script block 对应的 Beat、已选择素材、备选和缺口。计划固定精确 Blueprint revision，Blueprint 改变后旧计划不会继续伪装为 current。

**Independent test**：提交含 3 个 script blocks、2 个有效 material hints 和 1 个空缺的 Blueprint，物化计划后检查 3 个 Beat、2 个 assignment、1 个 Gap，以及精确 Blueprint binding。

**Acceptance scenarios**：

1. **Given** current Blueprint 含稳定 script block IDs，**When** 物化 baseline，**Then** 每个 block 恰好产生一个稳定 Beat，并保存文本 digest 而非复制第二份可编辑脚本真源。
2. **Given** Blueprint head 已变化，**When** 客户端用旧 binding 物化或读取 current plan，**Then** Core 返回 stale/conflict，不静默沿用旧计划。
3. **Given** 某 block 没有 verified material hint，**When** 物化，**Then** 产生明确 Gap；系统不补入低质量或无关素材。

### US2：Assignment 只能引用 Core 可证明的素材（P1）

Coverage Plan 中的选材必须回到稳定 asset 或精确 span。Core 在同一事务内解析当前 source/analysis head，固定无路径的 proof manifest；Agent 或 UI 不能伪造 asset hash、时间段或 analysis revision。

**Independent test**：分别使用可用 image asset、current video span、过期 span、missing source 与伪造 evidence ref，验证只有前两者可成为 selected assignment。

**Acceptance scenarios**：

1. **Given** `memolens://evidence/span/...` 指向 current succeeded analysis，**When** 物化，**Then** manifest 固定 asset SHA、analysis run/revision 与 `[start_ms,end_ms)`。
2. **Given** source unavailable 或 analysis head 已变化，**When** 物化，**Then** evidence 保持 unresolved 并进入 Gap/alternative reason，不成为 selected assignment。
3. **Given** evidence proof 含本地路径，**When** contract validation，**Then** 写入被拒绝；canonical plan 不泄露 Library locator。

### US3：同一输入可重放，失败不留下半个 current（P1）

同一 Blueprint 和相同 evidence head 必须产生相同 plan content digest。revision/head、operation 与 idempotency receipt 原子提交；进程失败或验证失败时 current head 不改变。

**Independent test**：重复、并发、故障注入和重启后重试同一命令，检查 digest、CAS 和数据库一致性。

**Acceptance scenarios**：

1. **Given** 相同 exact Blueprint/evidence 输入，**When** 在两个干净数据库重放，**Then** canonical plan content digest 一致。
2. **Given** 相同 idempotency key 与相同请求，**When** 重试，**Then** 返回同一 receipt；相同 key 不同请求返回 conflict。
3. **Given** revision 已由另一客户端推进，**When** 旧 expected head 提交，**Then** 命令失败且没有 plan revision/head/operation 的部分写入。

## Functional requirements

- **FR-001**：Coverage Plan 必须是独立、版本化、不可变的 canonical entity；不得塞回 Blueprint 或只存在于 Timeline JSON。
- **FR-002**：每个 plan 必须固定 exact Blueprint `{revision, content_sha256, semantic_sha256}`。
- **FR-003**：A1 baseline 的 Beat 集合必须由 Blueprint `script.blocks` 确定性导出；一个 block 一个 Beat，顺序与 block 顺序一致。
- **FR-004**：Beat identity 必须由稳定项目/Blueprint/script-block 输入导出，重试不得生成随机 ID。
- **FR-005**：A1 timing 只能声明 `estimated_text`，不得声称 voice、pause、breath、music beat 或 action boundary 已分析。
- **FR-006**：baseline 可从 `material_hints` 形成 candidate/assignment；`reason` 是不可信说明，不得升级为 factual support。
- **FR-007**：首版 match type 固定为非事实性的 `depiction_unspecified`；未来 factual/mood/continuity taxonomy 必须由更高阶 Coverage proposal 明确产生并验证。
- **FR-008**：selected assignment 必须引用 verified evidence；unresolved evidence 只能成为 rejected alternative 或 Gap provenance。
- **FR-009**：video assignment 必须固定精确 span 和 current analysis proof；image assignment 固定 asset hash。
- **FR-010**：proof manifest 不得包含 canonical path、relative path、文件名、用户文本或凭证。
- **FR-011**：无可用 assignment 的 Beat 必须保留 Gap，并区分 `no_material_hint`、`evidence_unresolved`、`evidence_span_too_short` 与 `duplicate_evidence_reserved`。
- **FR-012**：baseline 必须避免无创作理由的同一 evidence 被自动选中多次；第一次按 Beat/hint 顺序保留，后续记录为未选 alternative。
- **FR-013**：预计时长必须由明确版本的确定性规则产生，且总目标来自 exact Blueprint output；没有目标时使用带版本的安全默认值。
- **FR-014**：plan content 不得包含生成时间或随机 UUID；时间戳只存在于存储元数据/operation，不参与领域 digest。
- **FR-015**：revision、head 与 operation 必须在一个 `BEGIN IMMEDIATE` 事务中提交，并以 expected plan head 做 CAS。
- **FR-016**：相同 request 的重试必须幂等；key 重用但 request digest 不同必须失败。
- **FR-017**：成功的 workspace read 必须区分 `missing`、`current`、`stale_blueprint` 与 `stale_evidence`，不能只取最大 revision；任何 ledger/contract/digest 损坏必须 fail closed 为稳定 `409 coverage_integrity_error`，不得伪装成可消费的 freshness 状态。
- **FR-018**：A1 不开放 raw SQL、自由 solver、自由 FFmpeg 或原文件写入；effect class 为 `reversible_project_write`。
- **FR-019**：当前 plan、exact revision 与 bounded history 必须可通过 Core read surface 读取；GET 保持本地只读访问，POST materialize 只经 Desktop token 写入，MCP 初始保持只读。
- **FR-020**：本切片不宣称 global assignment 优于 top-k；ML-018-B 通过预注册评测后才可晋级。

## Canonical entities

- **Coverage Plan Revision**：不可变 Beat/Need/Assignment/Gap 文档及 exact bindings。
- **Coverage Plan Head**：项目当前计划的唯一 head。
- **Coverage Operation**：物化/恢复/后续重算的不可变领域操作。
- **Evidence Proof**：Core 从 canonical ledger 解析出的无路径 asset/span 证明。
- **Plan Freshness**：计划相对 current Blueprint、analysis/source evidence 的可执行状态。

## Out of scope

- 自动 ASR、气口、静音、音乐 beat、动作峰值。
- Agent 自由提交任意 Coverage Graph。
- 全局优化、局部重算、locks/rejections。
- Technique compiler、Timeline lowering、render/export。
- 图数据库、向量库或新的模型账户。

## Success criteria

- **SC-001**：固定 fixtures 中 Beat 对 script block 覆盖率 100%，dangling block 为 0。
- **SC-002**：selected assignments 的 verified evidence 比例 100%，路径泄漏为 0。
- **SC-003**：同输入三次重放 content digest 一致率 100%。
- **SC-004**：并发 100 轮 CAS 无 silent overwrite；故障注入后 partial head/revision/operation 为 0。
- **SC-005**：no evidence fixture 的无关素材填充率为 0，Gap reason 准确率 100%。
- **SC-006**：A1 focused tests、schema migration/rollback tests 与整仓 gate 全部通过后，验证状态才可升级为 `LOCAL VALIDATION WITH RESIDUALS`；Desktop 视觉、remote CI、发布与跨进程 whole-database rollback 仍需分别取证，不能由本地 gate 代替。
