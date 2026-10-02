# Feature Specification: Canonical Timeline Structural Edit

- Feature ID：`ML-015-B2B4C`
- 创建日期：2026-08-29
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCALLY VALIDATED / PARENT B2B4 REAL-HOST JOURNEYS PENDING`
- 父规范：[ML-015 Agent-Agnostic Creative Protocol](../../spec.md)
- 前置切片：[ML-015-B2B3 Canonical Timeline Revision Edit](../015-b2b3-canonical-timeline-revision-edit/spec.md)、[ML-015-B2B4 Codex / DeepSeek Canonical Editor Handoff](../015-b2b4-codex-deepseek-canonical-editor-handoff/spec.md)、[ML-015-B2C0 Canonical Timeline History + Restore](../015-b2c0-canonical-timeline-history-restore/spec.md)

> 本文是规范合同；实现与已观察的证据见 [Implementation Evidence](implementation-evidence.md)。当前 source worktree 已有 structural action、Timeline v2、V18 migration 和共用 Browser Split/Remove；production inventory、final-version plugin discovery、fresh Codex reinstall/cache parity、official DeepSeek loader、独立审查、final-diff `npm run check` 与 tracked/untracked whitespace gates 均已通过。Remote CI/release 与 B2B4 T053/T054 真实宿主旅程仍未闭合。

## Why this slice exists

B2B4C 之前，B2B3/B2B4 只允许四种 closed canonical edit：

- `move_clip`
- `trim_clip`
- `set_clip_duration`
- `replace_clip`

Canonical Editor 原有 Stage → Save → canonical reread、CAS、receipt、capability 和 append-only revision 语义，却没有用户可见的 canonical Split/Remove。本切片现已在同一页面加入两个结构操作；旧 `memolens_editor_handoff` 仍属于 **Unsaved Draft Lab / Not saved / process-scoped**，不具有 canonical ledger、receipt、CAS 或持久权限，也没有被接入 canonical write path。

本切片只交付最小结构编辑闭环：

```text
canonical Timeline head N
  + separately authorized structural intent
  + exactly one closed split_clip or delete_clip
  + verified current Blueprint/Coverage/source/head facts
  → deterministic Timeline schema v2 result
  → atomic operation/revision/head/receipt/use append
  → canonical reread of N+1
```

## Audited baseline and incompatibility

### Current Timeline v1 cannot represent the result

当前 Timeline v1 admission 要求 visual clip、Coverage Beat、assignment 与 evidence 一一对应：clip 数量等于 Beat 数量，Beat ID 集合相同，assignment/evidence 唯一。结构编辑会有意破坏该基数：

- Split 产生两个 occurrence，共享原 Beat、assignment、evidence 与 source proof，但 source interval 不同。
- Delete 使一个 Coverage Beat 暂时没有 visual occurrence。

因此不得把两个 op 只加入现有 `_EDIT_FIELDS` 后继续把结果标成 schema v1。

### Existing edit capability cannot silently gain delete authority

`timeline.apply_edit` 已是可签发、可持久使用的 project-bound capability。把 delete/split 加入该 action 会让既有 capability 在没有重新批准时获得更具破坏性的 project mutation。结构编辑必须使用独立 action：

```text
timeline.apply_structural_edit
```

拥有 `timeline.apply_edit`、`timeline.restore_revision` 或 preview capability 都不隐式拥有该 action；反向也成立。

### Shared Browser UI remains the primary surface

Codex plugin 与 DeepSeek Harness 必须继续打开同一个 project-bound `canonical-editor.html` 和同一个 loopback editor server。Electron Timeline 工作台不是本切片的主编辑界面，但所有 native/React Timeline readers 必须能读取 schema v2，不能因 Browser 成功保存结构 revision 后崩溃或误报 canonical corruption。

## Goals

1. 为一个已选 canonical visual occurrence 提供 video-only Split。
2. 为一个已选 visual occurrence提供 Remove from Timeline，原始媒体不变。
3. 以独立 capability、closed request、CAS、idempotency、receipt 和 cold replay 保存 N+1。
4. 引入 Timeline schema v2，同时完整保留 v1 历史和 v1/v2 restore。
5. 让 preview、export、usage 和所有 readers 对合法结构结果保持一致。
6. 保持 Codex/DeepSeek 单一 UI/服务实现，不复制 host-specific edit 逻辑。

## Non-goals

- image/audio split、speed ramp、transition、crop、keyframe、multi-track 或 ripple mode 选择。
- 删除原始媒体、source row、evidence、Coverage Beat 或 Blueprint assignment。
- bulk operations、caller-supplied result Timeline 或 generic JSON patch。
- 完整 undo/redo/branch/merge；只复用 B2C0 append-only restore。
- 把 Unsaved Draft Lab 升格为 canonical authority。
- 用受控 fixture 代替 B2B4 T053/T054 真实 Codex/DeepSeek host/model/UI journey。

## Authority and API contract

### Capability

- 新 action：`timeline.apply_structural_edit`。
- effect class：durable project mutation。
- scope 必须绑定 exact `database_uuid + project_id`，并显示 current Timeline head。
- capability issuance/revoke/expiry/exhaustion、single-use nonce、HMAC proof、receipt 与 use event 复用既有 paired authority 原语。
- action set 仍是 closed、排序、无 wildcard；是否与 edit/restore 一起签发必须由显式用户批准决定。

### Endpoints

- Paired agent：`POST /v1/agent/creative/projects/{project_id}/timeline/structural-edit`
- Desktop compatibility：`POST /v1/creative/projects/{project_id}/timeline/structural-edit`

两条路径共用同一个 Core structural contract 和 Timeline service mutation。Paired 路径额外绑定 exact action/path/body/nonce/proof；Browser 不取得 pairing secret、main secret、desktop secret、文件路径或 caller-selectable actor/origin。

### Closed request

每个请求的顶层命令字段只允许一个 `structural_edit` object，不接受顶层 `edit`、operations array 或 caller result Timeline。请求继续绑定现有 Timeline command 所需的 exact database/project/current head/Blueprint/Coverage/source/idempotency facts。服务端派生 actor、origin、result Timeline、source bindings 和 child IDs。Core 纯函数内部参数名可为 `edit`，但它不是 HTTP/receipt 顶层字段。

## Closed structural edit dialect v1

### `split_clip`

Exact `structural_edit` value：

```json
{
  "op": "split_clip",
  "clip_id": "clip_...",
  "source_split_ms": 1234
}
```

Admission rules：

1. 只接受以上三个字段；未知字段、缺字段、float、boolean-as-integer 和非规范字符串全部拒绝。
2. target 必须是 current head 中唯一存在的 video visual clip；第一版拒绝 image 和 audio。
3. `source_split_ms` 是绝对 source coordinate，不是 Draft Lab 的 `offset_ms`。
4. split point 必须严格位于当前 `[source_in_ms, source_out_ms)` 内。
5. 左右 child 的 source duration 都必须至少 `100ms`。
6. 结果 visual clip 总数不得超过 `256`。
7. 左右 child 必须精确组成 `[source_in_ms, source_split_ms)` 和 `[source_split_ms, source_out_ms)`，无 gap、overlap、rounding 或 source substitution。
8. 两个 child 保留原 Beat、assignment、evidence、asset、proof 和非结构属性；各自 source binding 改为对应 child clip ID 与 interval。
9. child clip IDs 必须由 Core 现有 canonical video ID derivation 基于 child source ranges 确定性生成，不允许 Browser、host、随机数或路径参与。任何既有 ID collision 必须 fail closed。
10. Timeline 总 duration 不变；后续 clip 的 Timeline positions 不变。两个 child 的相邻 Timeline positions 由原 clip 起点和 child durations 确定。

### `delete_clip`

Exact `structural_edit` value：

```json
{
  "op": "delete_clip",
  "clip_id": "clip_..."
}
```

Admission rules：

1. 只接受以上两个字段；不存在、重复或歧义 target 全部拒绝。
2. target 可以是 current canonical visual track 的 image 或 video occurrence。
3. 禁止删除 Timeline 中最后一个 visual clip。
4. 只删除该 occurrence 和它的 exact source binding；不得删除或改写 source media、asset、evidence、Coverage 或 Blueprint。
5. 后续 clip 的 ordinal 与 Timeline positions 确定性前移；Timeline 总 duration 精确减去被删除 clip 的 duration。
6. 非目标 occurrence 的 source identity、source ranges、proof 和 clip IDs 保持不变。

## Canonical Timeline schema v2

### Shape admission

V18 必须让 revision row 显式接受 Timeline schema `"1"` 和 `"2"`。v2 保留 canonical Timeline 的单一 non-empty primary visual track 与 source binding 结构，并改变 occurrence 基数规则：

- visual clip 数量为 `1..256`。
- `clip_id` 继续全局唯一。
- 每个 clip 恰有一个 clip-bound source binding。
- Beat、assignment 和 evidence 可以在 split 后重复，也可以在 delete 后没有 occurrence。
- 每个仍存在的 occurrence 必须映射 current pinned Coverage 中的 Beat/assignment/evidence，并验证 exact asset/source/proof。
- 同一 evidence 的多次 occurrence 必须保持一致的 asset/source/proof identity；只有 clip ID 和合法 source interval 可以不同。

Schema validator 只证明 v2 shape。v2 revision 的 canonical admission 还必须由 operation ledger 从父 revision exact replay 证明；不得接受 caller 直接提交任意 v2 Timeline。

### Version transitions

- 普通四-op edit 在 v1 head 上继续产出 v1，在 v2 head 上必须保持 v2，并通过 v2 occurrence validation。
- 第一次 `split_clip` 或 `delete_clip` 从 v1 head 产出 v2。
- structural edit 在 v2 head 上继续产出 v2。
- materialize/reconcile 继续产出 v1，不因本切片改变编译器语义。
- Restore 必须精确恢复历史 K 的 schema version：v1→v1、v2→v2，且允许 current v2 head append 一个内容来自 historical v1 的新 v1 revision，反之亦然。

## V18 migration contract

本切片以当前 V17 为前置，新增 immutable V18 migration，不修改 V1–V17 migration row、checksum、历史 JSON、digest、receipt 或 event hash。V18 至少需要：

1. 重建 `canonical_timeline_revisions` 的 schema-version CHECK，使其只接受 `1 | 2`。
2. 重建 `canonical_timeline_operations` 的 closed command union，加入 `timeline.apply_structural_edit`，command version 仍为 `1`。
3. 扩展 `agent_project_capabilities` 的 closed action-set CHECK。
4. 扩展 `agent_project_command_receipts` 的 paired canonical Timeline typed branch，保留 generic exact-request v2 receipt lane。
5. 扩展 `agent_project_capability_events.action` union 与相应 semantic cardinality validator。
6. 同步 Core 与 standalone plugin schema manifest、normalized SQL、checksum、future-schema refusal、cold prefix commitment、resource budgets 和 production-surface inventory。
7. 使用受限 backup 与单事务 copy/verify/swap；任何 fault、collision、count/digest mismatch 必须回滚到字节可验证的 V17 数据库。

## Transaction, CAS and replay

结构保存必须在一个 verified transaction 中：

1. 重读 project、current Timeline head N、current Blueprint/Coverage 和 fixed source facts。
2. 验证 paired capability definitive state，或验证 desktop authority。
3. 用 Core contract 从 N 派生唯一 N+1；不接受 caller result。
4. CAS current head=N。
5. 追加 structural operation、revision N+1、head、paired receipt、capability use/event。
6. 运行 exact successor/cold-replay admission。
7. commit 后通过 canonical reader 重读并核对 full head、operation、selected resource 和 result digest。

同一 idempotency identity + 同一 exact request 返回冻结结果且不新增行、不重复消耗 capability；同 identity + 不同 request 必须 conflict。response loss 复用同一 identity reconciliation，不生成第二个 split/delete。

## Restore and history

- B2C0 `timeline.restore_revision` 保持独立 capability；structural capability 不隐式授予 restore。
- history projection 必须可读取 v1/v2 混合 revision chain。
- restore K as N+1 继续只重包 K 的 revision/parent，并保留 K 的 Timeline schema version 和 exact source bindings。
- cold audit 必须按 operation type dispatch：普通 edit、structural edit、restore 都从真实父 revision独立重放。
- 删除后的历史仍可 restore；restore 不复活或修改 source media，只恢复 canonical occurrence 引用。

## Preview, export and usage

### Preview

结构 Stage 后，旧 preview lease 的 clip scope 已可能失效，server 必须撤销该 lease。Discard 后根据 current canonical head 重签；Save+reread 后根据新 child/remaining clip IDs 重签。Browser 不能自行扩大 lease scope。

### Export actual-read proof

现有 export actual-read validator 把重复 `evidence_ref` 当作错误；合法 split 必然产生两个共享 evidence 的 occurrence。V2 export 必须：

- 允许同一 evidence 出现多次，但要求 asset/source/proof identity 完全一致。
- 逐 occurrence 记录唯一 clip ID 与 exact child source interval。
- 拒绝 evidence→asset/source/proof substitution、错误 child range 和 duplicate clip ID。
- 不把 source interval overlap 一概判错；未来合法 trim/reuse 可能重叠，安全边界是 identity/proof/range 与 ledger 一致。

Usage occurrence projection 必须保留重复 occurrence 和各自 half-open intervals；delete 后 Export/Usage 不得包含已删除 occurrence。

## Codex / DeepSeek Canonical Editor UI

`canonical-editor.html` 是第一优先级实现面：

- 选中 video clip 且 playhead 位于可合法切分区间时显示并启用 **Split at playhead**。
- UI 将 playhead 映射为 `source_in_ms + (playhead_ms - clip.timeline_start_ms)`，server 使用 current canonical state重新验证。
- 选中 image/video 时显示 **Remove from Timeline**；最后一个 clip、已有互斥 pending 或只读 historical selection 时禁用。
- Remove 附带固定提示：`Removes this occurrence from the Timeline. Original media remains unchanged.`
- 点击只产生 `Pending · not canonical` Stage；只有显式 Save 才请求 N+1。
- Save 后 exact reread；conflict/stale/unknown-commit 不自动 rebase、不乐观显示 Saved。
- regular-edit-only pairing 可只读看见结构能力说明，但不能 stage/save；structural-only pairing不能调用四种普通 edit 或 restore。

Codex plugin 与 DeepSeek Harness 的 manifest/prompt/skill/README 只描述同一 handoff URL 和同一 authority。不得创建 DeepSeek-only 或 Codex-only split/delete implementation。

## Electron compatibility boundary

- `timelineTypes.ts`、`timelineModel.ts`、`timelineEditModel.ts`、`timelineApi.ts` 和 workspace reader 必须能解析合法 v2 head、重复 assignment/evidence occurrence 和 v1/v2 history。
- Electron authority presentation 必须显示并审批/revoke `timeline.apply_structural_edit`。
- Electron Split/Remove 按钮可以后置；Browser Canonical Editor 是本切片的产品验收面。
- 即使 Electron 无结构按钮，也不得拒绝、改写或降级 Browser 已保存的 v2 canonical head。

## Unsaved Draft Lab isolation

Canonical implementation 不得：

- import/call `memolens_timeline.py::revise_timeline_draft`；
- 接受 Draft Lab 的 `offset_ms`、bulk `operations`、draft ID 或 caller Timeline JSON；
- 因 legacy UI 支持 image/audio split 就放宽 canonical video-only contract；
- 把 process-scoped history、draft preview 或重启前状态写入 canonical ledger。

可以借鉴 Split/Delete 的可见交互概念，但 Core contract、IDs、proof、CAS、receipt 和 replay 必须独立实现。兼容测试必须证明 Draft Lab 进程重启后临时状态消失，且使用 Draft Lab 前后 canonical revision/operation/head/receipt cardinality 不变。

## Stable error semantics

| Code | Layer and meaning |
| --- | --- |
| `invalid_timeline_structural_edit_command` | service 顶层 command shape 不是 exact `expected_* + structural_edit` |
| `invalid_timeline_structural_edit` | HTTP 的 structural contract 外层错误；精确 Core 原因在 `details.errors[0].code` |
| `invalid_structural_edit` / `unsupported_structural_edit` / `invalid_clip_id` | Core closed op shape、op union 或 clip identity 无效 |
| `unknown_clip` / `invalid_split_target` | target 不在 exact parent，或 split target 不是 video occurrence |
| `invalid_source_split` / `split_child_too_short` | absolute source split 类型/边界无效，或任一 child 小于 100ms |
| `timeline_clip_limit_exceeded` | 结果超过 256 clips |
| `cannot_delete_last_clip` | 请求删除最后一个 visual clip |
| `split_identity_collision` | 两个确定性 child ID 不唯一，或与结果中现存 ID 冲突 |
| `invalid_parent_timeline` / `invalid_coverage_plan` / `coverage_binding_mismatch` | parent Timeline/Coverage/source proof 不是 exact current binding |
| `invalid_structural_edit_result` / `timeline_structural_edit_replay_mismatch` | 派生结果无法通过 current validator，或持久结果无法从 exact parent+op 重放 |
| `revision_limit_exceeded` | successor revision 超出 canonical 上限 |
| `timeline_head_conflict` | expected N 不再是 current head |
| `timeline_idempotency_conflict` | 同 identity 绑定不同请求 |
| `agent_pairing_scope_denied` | capability 不含 structural action |
| `canonical_editor_post_commit_reconciliation_required` | commit 可能成功但 exact reread 尚未确认 |

## Functional requirements

- **FR-B2B4C-001**：只通过独立 `timeline.apply_structural_edit` authority 保存结构 mutation。
- **FR-B2B4C-002**：structural dialect v1 只包含 exact `split_clip` 和 `delete_clip`，每次一个 op。
- **FR-B2B4C-003**：split 第一版仅支持 video、100ms minimum、256 clip cap、确定性稳定 child IDs 和 exact interval partition。
- **FR-B2B4C-004**：delete 禁止删除最后一个 visual clip，且永不删除/修改 source media。
- **FR-B2B4C-005**：Timeline schema v2 显式表达重复或缺失 occurrence；任意 v2 canonical admission 必须有 ledger replay 证明。
- **FR-B2B4C-006**：V18 migration 扩展 revision/operation/capability/receipt/event closed unions，并保持 V1–V17 历史字节与 digest。
- **FR-B2B4C-007**：structural save 与 CAS、revision、head、receipt、capability use/event 同事务原子提交。
- **FR-B2B4C-008**：same-request replay 不重复写入或消耗；different-request conflict fail closed。
- **FR-B2B4C-009**：普通四-op edit 可继续操作合法 v2 head，且不得获得 structural authority。
- **FR-B2B4C-010**：restore 支持 v1/v2 混合历史并保留 historical schema/version/source bindings。
- **FR-B2B4C-011**：export 接受 proof-consistent duplicate evidence occurrence，拒绝 substitution；Usage 保留 occurrence-level half-open intervals。
- **FR-B2B4C-012**：Codex/DeepSeek 使用同一 canonical editor/server，UI 提供 Stage/Discard/Save Split/Remove。
- **FR-B2B4C-013**：Electron readers 和 authority presentation 与 v2/action 兼容，但 Electron 编辑按钮不是主验收门槛。
- **FR-B2B4C-014**：Canonical 与 Unsaved Draft Lab 的数据、API、方言、authority 和 history 保持隔离。
- **FR-B2B4C-015**：pre-commit/post-commit uncertainty、stale、revoke、expiry、restart 和 concurrent CAS 都有可恢复、可证伪语义。
- **FR-B2B4C-016**：B2B4 T053/T054 真实宿主门槛继续独立保留，不由本切片 focused/fixture evidence 替代。

## Required validation matrix

| Lane | Required falsification |
| --- | --- |
| Pure split | exact left/right ranges；total duration unchanged；stable child IDs；binding duplication；v1→v2；100ms edges；video-only；256 cap；collision |
| Pure delete | exact target/binding removal；ripple ordinals/timing；duration subtraction；last-clip refusal；source rows unchanged |
| Closed parser | unknown/missing fields；bool/float；legacy `offset_ms`；bulk array；caller result/source/actor/origin refusal |
| V1/V2 validation | v1 uniqueness remains strict；v2 valid occurrences accepted；arbitrary/tampered v2 rejected by cold replay；normal four-op edit remains valid on v2 |
| V18 migration | fresh/populated V17→V18；backup；copy/swap faults；name collision；future schema；old row/JSON/digest/receipt/event/hash preservation；Core/plugin parity |
| CAS/idempotency | same request zero extra rows；different request conflict；concurrent single winner；stale DB/project/head/Blueprint/Coverage/source zero writes |
| Atomic fault | failure after operation/revision/receipt/use/head stages leaves no partial mutation；cold reopen exact audit |
| Tamper/security | wrong child interval/binding/proof/delete target；wrong action/path/body/project/nonce/origin/secret；revoked/expired/exhausted capability |
| Restore/history | split/delete K restored as N+1；mixed v1/v2 chain；structural-only cannot restore；tampered historical v2 fails closed |
| Preview | Stage revokes stale lease；Discard remints current scope；Save+reread remints child/remaining scope；Browser cannot broaden scope |
| Export/Usage | consistent duplicate evidence accepted；substitution rejected；two child occurrences/intervals emitted；deleted occurrence absent |
| Canonical UI | Split/Delete enablement；playhead mapping；pending noncanonical；Discard；Save exact reread；conflict/unknown commit recovery |
| Browser security | one-time bootstrap/session/Host/Origin/CSP/method/content-type/body/TTL/count；no path/source/secret exposure |
| Draft isolation | canonical page has no `offset_ms`/bulk/draft call；Draft restart loses state；canonical table cardinality unchanged |
| Host parity | Codex/DeepSeek share exact handoff URL/code path；controlled two-host fixture；B2B4 T053/T054 remain separately pending until real runs |
| Repository gates | focused warnings-as-error；production oracle/inventory；plugin validator/reinstall/cache parity；Node/renderer/typecheck/build；`npm run check`；`git diff --check` |

## Promotion boundary

本切片已达到 current shared-worktree 的本地完整验收：production inventory/oracle、plugin 最终验证与 fresh reinstall/cache parity、full repository gate 和明确 evidence 均已闭合。这一结论只覆盖本地 source/runtime 证据；B2B4 T053/T054 的 fresh Codex→DeepSeek 与 DeepSeek→Codex 真实 host/model/UI journeys 仍是独立 promotion blocker，Remote CI/release 也未执行。adapter-process、受控 Browser fixture、截图或静态文档都不能代替。
