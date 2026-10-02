# Feature Specification: Codex / DeepSeek Canonical Editor Handoff

- Feature ID：`ML-015-B2B4`
- 创建日期：2026-08-23
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE / PROMOTION BLOCKED`
- 验证状态：`LOCAL REPOSITORY GATES PASSED; REAL-HOST JOURNEYS PENDING`
- 父规范：[ML-015 Agent-Agnostic Creative Protocol](../../spec.md)
- 前置权限切片：[ML-015-B1A Authority Runtime Hardening](../015-b1a-authority-runtime-hardening/spec.md)
- 前置剪辑切片：[ML-015-B2B3 Canonical Timeline Revision Edit](../015-b2b3-canonical-timeline-revision-edit/spec.md)
- 当前边界：V11 paired Timeline write、V12 versioned generic receipt convergence、V13 restore extension、Canonical Editor 和 Codex/DeepSeek 双入口已在 shared worktree 实现并通过本地整仓 gate；fresh 双向真实 cross-host model/UI journey 仍未完成，因此本切片不能 promotion
- 实施证据：[implementation-evidence.md](implementation-evidence.md)

## Why this slice exists

MemoLens 已有一条能从 Codex 和 DeepSeek Harness 打开浏览器页面的 Timeline 1.0 editor handoff，但它的 Timeline 由调用者传入，修订只保存在当前 MCP 进程内存。它是有用的试剪工具，却不是 canonical project 的编辑器，也不能被描述为已保存、可恢复或可导出。

B2B3 定义了 canonical Timeline 上第一组四种 closed edit，但编辑入口仍在 MemoLens React 工作台。B2B4 把同一条命令链接入 Codex 插件和 DeepSeek Harness：

```text
Codex plugin / DeepSeek Harness
  → memolens_canonical_editor_handoff(project_id)
  → one-time loopback browser handoff
  → server-derived canonical project + exact Timeline head N
  → one B2B3 edit staged in the browser
  → explicit Save
  → paired timeline.apply_edit/v1 + exact CAS
  → permanent paired provenance + Timeline revision N+1
  → canonical reread

Codex later opens project P ─┐
                              ├→ the same database_uuid / project_id / head
DeepSeek later opens project P ─┘
```

两个 host 不共享聊天上下文、浏览器内存或 host-local Timeline。它们只通过同一 `database_uuid + project_id + canonical head` 接续。Electron 工作台不是 B2B4 的主编辑界面；Electron 只保留 main-owned pairing approval/revoke 和必要的本地 runtime 管理责任。

## Product boundary

### One canonical project, two thin host adapters

- Codex 与 DeepSeek 使用同一 raw MCP 工具合同；DeepSeek 只增加它的 server-qualified 工具名和 tool-card 展示。
- host adapter 不实现 edit、source resolution、CAS、receipt 或 Timeline 持久化逻辑。
- canonical handoff 只接受 `project_id`；不接受 caller-supplied Timeline JSON、replacement candidates、source identity、本地路径或预言结果。
- handoff server 通过只读 canonical reader 导出当前 Blueprint、Coverage、Timeline 和 replacement eligibility。任何保存仍由 Core 在事务内重读并作决定。
- 页面只允许 B2B3 的 `trim_clip`、`set_clip_duration`、`move_clip`、`replace_clip`；不把 legacy editor 的其他按钮暗中转成 canonical 命令。

### Legacy editor stays, under an honest name

现有 `memolens_editor_handoff` 及 Timeline 1.0 进程内编辑器保留为 **Unsaved Draft Lab**：

- 页面、工具说明、Codex skill、DeepSeek skill 和 README 都必须持续显示 `Not saved / process-scoped`。
- 它不能获取 `timeline.apply_edit` capability，不能将进程内 draft 自动导入 canonical ledger。
- canonical editor 不接受 legacy `timeline_draft_id`、Timeline JSON 或内存 history。
- 两个工具在名称、状态和恢复语义上不得混淆。

## Authority and provenance

### Project-bound pairing

`timeline.apply_edit` 是 `reversible_project_write`，但它不是 semantic confirmation，也不授予 render、export、publish、文件系统或任意 Timeline 写权。

一个可用 capability 必须精确绑定：

```text
database_uuid
+ runtime_authority_epoch
+ project_id
+ paired_subject_id
+ explicit action set containing timeline.apply_edit
+ expiry
+ max_operations
+ proof-secret possession
```

现有 nonce/HMAC 合同继续绑定 capability、single-use nonce、HTTP method、canonical path、database UUID、project、canonical body digest 和 Idempotency-Key。将同一 proof 换到另一项目、action、body、path 或 key 必须失败。

Browser 不得取得 pairing proof secret、main authority token 或 desktop session token。页面 Save 只把 closed edit 提交给当前 loopback editor session；保存在插件进程内的 credential client 负责 nonce/proof 和 paired API 调用。secret 不得进入 URL、HTML/JS、DOM、MCP result、HTTP response、日志、argv 或 env。

### Permanent paired Timeline provenance

成功的 canonical editor save 不能伪装为 Desktop edit。Core 必须从已验 capability 推导 actor/origin，并持久下列闭包：

```text
exact capability use
  ↔ permanent paired project-command receipt
  ↔ exact timeline.apply_edit operation
  ↔ canonical Timeline revision N+1
  ↔ exact resulting head
```

actor 必须包含 `capability_id`、`paired_subject_id`、claimed client label 与明确的 `vendor_identity_verified=false` / `user_authority_verified=false`。origin surface 使用封闭的 host-neutral `agent_plugin_editor`；Codex/DeepSeek 标签不作为安全身份。Browser 中的 Save 点击是用户交互事实，不能写成 main-native gesture 或 Creative Blueprint semantic authority。

同一 receipt identity 与同一 request 重放只返回冻结响应，不重复消耗 operation；同 key 不同 request 必须 conflict。capability 过期、撤销、耗尽、runtime epoch 变化后不得开始新写，但之前完成的 paired Timeline revision 仍必须可验证、可读取。

## V11 origin and completed V12 convergence contract

V10 只为 Desktop-authenticated `timeline.apply_edit/v1` 增加 canonical Timeline edit vocabulary；V5 的 capability actions、Blueprint-specific paired receipts 和 use-event foreign keys 不能表达 paired Timeline command。B2B4 因此必须是显式 **V11 schema/authority migration**，不得只改 Python allowlist 或伪造 desktop receipt。

V11 交付 `timeline.apply_edit`、generic v2 Timeline receipt 和同一 capability-event sequence，但当时旧 Blueprint receipt 仍留在 legacy v1 表，只是可运行的过渡实现。

历史保存 oracle 证明：旧 Blueprint receipt 只保存 request digest，部分 no-change/restore 请求正文无法从 operation/revision 反演。把这些历史 row 伪装成拥有 exact `request_json`、新 actor/origin 和 v2 digest 会制造不存在的证据，并级联改写 event parent hash。因此没有原地重冻 V11，而是以显式 **V12 convergence migration** 完成 T011。

V12 已收敛为一张 versioned resource-neutral paired project-command ledger，而不是把 v1 假装成 v2：

1. capability action closed union 扩展为 `blueprint.commit_proposal | blueprint.restore_revision | timeline.apply_edit`，数组必须非空、唯一、合同顺序化；不允许 wildcard。
2. `agent_blueprint_command_receipts` 的历史 17-column projection、canonical JSON bytes、request/response/receipt digests 原样迁入 V12 `agent_project_command_receipts` 的 `receipt_schema_version=1` 分支；缺失的请求正文必须保持 `NULL` 并标记 `digest_only_legacy`，禁止合成。
3. `agent_project_capability_events` 保留单一 per-capability sequence，但重建其 action check 和 receipt/typed-operation linkage，使 Blueprint 与 Timeline use 不会产生并行计数器。
4. 既有 Blueprint capability、receipt、use event 的 ID、顺序、canonical JSON、request/response/receipt/event digest 和语义链全部保持；v1 event 只重定向 foreign key，不重签、不伪造新的 native approval或新 provenance。
5. V11 已有的 Timeline v2 receipt 在 V12 中保持 exact request/actor/origin/digest，并精确链接 canonical Timeline operation 与 resource identity；每个 paired operation 恰好有一条 receipt 和一条 capability-used event，反向也成立。
6. pairing receipt 的既有 observed Blueprint binding 保留；对包含 Timeline action 的新 presentation，其 canonical presentation JSON/digest 还必须包含 observed Timeline head，批准时不信任 renderer/Agent 提供的 head。
7. V11→V12 migration 采用本地受限 backup + 单事务 copy/verify/swap + exact physical-schema manifest；V1–V11 migration rows、领域 revision/operation bytes 和历史 digest 不改写。
8. Core 和 standalone plugin 必须使用同一冻结 V12 table/index/trigger/checksum 合同和 future-schema refusal。一侧认可、另一侧降级读取不允许。
9. V12 immutable tables 继续加入 cold exact schema allowlist、historical-prefix commitment、warm successor vector、resource budgets 与 UPDATE/DELETE triggers；旧 receipt 表在逐行验证后删除，不能作为第二 authority table 保留。

V12 generic row 必须是 closed versioned union：v1 诚实表达 `request digest only`，v2 才表达 exact request/actor/origin。若实现要求每个历史 row 都拥有 v2 正文与 digest，则该要求不可实现，必须 fail closed，而不是伪造数据。

## Canonical editor handoff contract

### Open

`memolens_canonical_editor_handoff` 请求只含一个 canonical `project_id`。插件进程必须：

1. 以只读方式识别 exact database UUID、未归档项目、current Blueprint/Coverage/Timeline heads 与 Timeline freshness。
2. 只对 current、可编辑的 canonical Timeline 创建 bounded in-memory session。
3. 返回随机 loopback port 上的短时 one-time bootstrap URL；不返回 source locator、文件路径、pairing secret 或 authority token。
4. 由 Codex in-app Browser 或 DeepSeek tool-card/fallback link 经用户手势打开；不声称自动弹窗。

one-time bootstrap 成功后必须失效，后续请求使用不出现在 URL 的 session credential。server 继续执行 loopback-only bind、Host/Origin 校验、strict methods/content types/body limits、CSP `default-src 'self'`、无任意路径和有界 session/TTL 等已有 editor handoff 安全边界。

### Stage and Save

- Browser 中的 pending preview 不是 canonical resource，不得进入 render/export/Usage。
- 一次 Save 只提交一个 B2B3 closed edit 与 session 开启时/last reread 的 exact preconditions。
- `replace_clip` 只提交 `assignment_id`；source binding 和 proof 由 Core 从 pinned Coverage 重新解析。
- 插件进程不得把本地 preview Timeline 作为 result document 发给 Core。
- 保存成功后页面必须通过 canonical reader 重读，并只在 `database_uuid/project_id/new head/selected head/operation` 全部一致时显示已保存。
- 页面不得用乐观 local state 合成 N+1，不得在 conflict 后自动 rebase 或将旧 edit 施加到新素材。

## Failure atomicity and recovery

- definitive capability check、exact CAS、Timeline operation/revision/head、paired receipt 和 capability-used event 必须在同一 `BEGIN IMMEDIATE` 事务成功或全部回滚。
- nonce/proof 路由层的成功只是 transport admission；Core transaction 内必须再验 database/project/action/epoch/expiry/revoke/max-use/secret 和 Timeline preconditions。
- 同一 expected head 上的两次 Save 只能有一次创建 N+1。另一次返回 `timeline_head_conflict`，0 条部分 operation/revision/receipt/use event。
- response 丢失时，同 key/同 request 重试先验证永久 receipt，返回冻结结果，再 canonical reread；不凭 HTTP timeout 推断未提交。
- pairing 未批准、错 scope、过期、撤销、耗尽或 runtime restart 时，页面保留仅内存 pending edit 并明确要求重新 pairing；不得移动 canonical head。
- Blueprint/Coverage/source/head 变 stale 时 fail closed。恢复方式是丢弃或显式重新检查后再编辑，不是隐式换素材。
- browser/MCP 进程在提交前崩溃，pending edit 可丢失但 canonical 不变；提交后崩溃则以 permanent receipt + canonical reread 为准。
- V11 migration 任一 copy、digest、foreign-key、schema manifest 或 backup 步骤失败时，原 V10 数据库保持可恢复且 V11 writer 不启用。

## User stories

### US1：在 Codex 中打开并保存 canonical 剪辑（P1）

**Given** 已配对的 Codex 插件和一个 current canonical Timeline N，**When** 用户通过 handoff 页面移动/裁剪/改图片时长/替换一个 clip 并 Save，**Then** Core 只追加 N+1，页面重读 N+1，旧 N bytes 不变。

### US2：DeepSeek 无聊天上下文接续同一项目（P1）

**Given** Codex 已把项目 P 保存到 N+1，**When** 新 DeepSeek Harness 会话只获得 `project_id=P` 并打开 canonical editor，**Then** 它看到 exact N+1，保存后得到 N+2，不创建 host-local 副本。

### US3：并发与凭据重放不静默覆盖（P1）

**Given** Codex 和 DeepSeek 同时从 N 开始编辑，**When** 两者保存，**Then** 只有一个 N+1；另一个显示 conflict 并要求 canonical refresh。响应丢失后使用同 idempotency identity 可准确恢复结果。

### US4：未配对或越权页面不能写（P1）

**Given** 只有 loopback URL、旧 bootstrap token、desktop token、伪造 host label 或一个不包含 `timeline.apply_edit` 的 capability，**When** 请求 Save，**Then** 在任何 Timeline/capability ledger mutation 前拒绝，secret 泄漏数为 0。

### US5：legacy Draft Lab 不冒充 canonical editor（P1）

**Given** 用户打开旧 `memolens_editor_handoff`，**When** 修订或重启 MCP 进程，**Then** 页面一直显示 Unsaved Draft Lab / Not saved，不产生 canonical revision、paired receipt 或恢复承诺。

## Functional requirements

- **FR-B2B4-001**：新增 project-bound `memolens_canonical_editor_handoff(project_id)`；不接受 caller Timeline、candidate list、path 或 source locator。
- **FR-B2B4-002**：Codex 与 DeepSeek Harness 共用同一 MCP implementation 和 canonical project reader；host 适配只负责展示 exact handoff URL。
- **FR-B2B4-003**：handoff 只能为未归档、存在 current canonical Timeline、Blueprint/Coverage/source 均 current 的项目创建。
- **FR-B2B4-004**：页面只支持 B2B3 四种 closed edit；一次 Save 只有一个 edit。
- **FR-B2B4-005**：pending preview 是 bounded process memory，不得被 render/export/Usage 或 project resume 当作 canonical state。
- **FR-B2B4-006**：Save 必须调用 paired `timeline.apply_edit/v1`；不得调用 Desktop route、伪造 desktop actor 或直接写 SQLite。
- **FR-B2B4-007**：V11 必须以 closed schema 表达 `timeline.apply_edit` capability、resource-neutral/typed permanent receipt 与 exact use-event linkage。
- **FR-B2B4-008**：V10→V11 必须备份、单事务、保存旧 rows/digests、精确校验 row counts/foreign keys/schema manifest，并对 future/tampered/collision schema fail closed。
- **FR-B2B4-009**：Core 与 standalone plugin 必须共享 V11 version/name/checksum/physical schema、resource budgets 和 future-schema refusal。
- **FR-B2B4-010**：pairing 必须绑定 exact database/runtime/project/subject/action/TTL/max-use；`timeline.apply_edit` 不能由 Blueprint-only capability 隐式获得。
- **FR-B2B4-011**：browser 页面不得获取 pairing secret、main/desktop token、nonce 生成权、source path 或任意 backend URL。
- **FR-B2B4-012**：one-time bootstrap、session credential、loopback bind、Host/Origin/CSP/method/content-type/body/TTL/session-count 边界必须有动态负向测试。
- **FR-B2B4-013**：Agent proof 必须绑定 exact method/path/database/project/body/idempotency key；nonce 只能消费一次。
- **FR-B2B4-014**：Core 必须在同一受管事务内重验 capability 并完成 Timeline operation/revision/head/receipt/use event。
- **FR-B2B4-015**：paired actor/origin 必须 server-derived；claimed Codex/DeepSeek/vendor 标签不是已验身份，Save 不是 semantic confirmation。
- **FR-B2B4-016**：exact CAS 必须覆盖 database UUID、project、Timeline head 和 pinned Blueprint/Coverage preconditions；不允许 last-write-wins。
- **FR-B2B4-017**：same key/same request 重放返回冻结 receipt 且不重复消耗；same key/different request 必须 conflict。
- **FR-B2B4-018**：成功 Save 后必须 canonical reread 并验证 full new head、selected resource 与 operation linkage；不得使用乐观 local N+1。
- **FR-B2B4-019**：conflict/stale/revoke/expiry/restart/response-loss 必须有可恢复状态，任一失败路径中部分持久行数为 0。
- **FR-B2B4-020**：Codex 保存后 DeepSeek 及 DeepSeek 保存后 Codex 都必须在无聊天传递下读到同一 head/digest。
- **FR-B2B4-021**：DeepSeek rich card 和 fallback link 只能使用 tool result 返回的 exact URL；不重写 URL，不声称自动打开。
- **FR-B2B4-022**：legacy `memolens_editor_handoff` 必须被明确标记为 Unsaved Draft Lab / Not saved，且不能进入 paired/canonical write path。
- **FR-B2B4-023**：Electron 仅保留 pairing approve/reject/revoke 与 runtime trust UI；B2B4 不把 Electron Timeline 工作台设为主入口或前置操作。
- **FR-B2B4-024**：MCP 不暴露通用无 UI Timeline write tool；canonical handoff 启动临时 UI，项目写只由该 session 的明确 Save 驱动。
- **FR-B2B4-025**：V11 cold/warm integrity 验证必须纳入新的 capability/receipt/event rows，不得用“页面已看到 N+1”代替 ledger replay 证据。

## Proposed stable errors

| Code | HTTP/surface | Meaning |
| --- | ---: | --- |
| `canonical_editor_project_required` | MCP | 缺失或非法 project identity |
| `canonical_editor_project_unavailable` | MCP | 项目不存在、已归档或不属于当前 database |
| `canonical_editor_timeline_unavailable` | MCP | 没有 current canonical Timeline 或上游/source 已 stale |
| `canonical_editor_handoff_expired` | Browser | bootstrap/session 过期 |
| `canonical_editor_session_invalid` | Browser | bootstrap 重放、错 session/Host/Origin/method/body |
| `agent_pairing_required` | 401 | 没有可用 paired capability |
| `agent_pairing_scope_denied` | 403 | capability 不覆盖该 database/project/action |
| `agent_pairing_expired` | 403 | capability 过期、耗尽或 runtime epoch 失效 |
| `agent_pairing_revoked` | 403 | capability 已撤销 |
| `agent_operation_nonce_invalid` | 403 | nonce 缺失、过期、错 scope 或已使用 |
| `agent_operation_request_mismatch` | 409 | proof 与 method/path/body/key 不一致 |
| `timeline_head_conflict` | 409 | expected canonical Timeline head 不再 current |
| `timeline_idempotency_conflict` | 409 | 同 receipt identity 绑定了不同 request |
| `blueprint_head_conflict` | 409 | pinned Blueprint 已变化 |
| `coverage_head_conflict` | 409 | pinned Coverage 已变化 |
| `coverage_plan_stale_evidence` | 409 | source/evidence 不再满足绑定 |
| `invalid_timeline_edit_command` | 422 | paired edit envelope 不是 closed v1 command |
| `invalid_timeline_edit` | 422 | edit 违反 B2B3 领域规则 |

稳定错误在实现时可以合并为已有更精确的等价 code，但不得用单一 `save_failed` 吞掉 pairing、CAS、stale、invalid edit 和未知提交状态的区别。

## Required test matrix

| Lane | Required falsification |
| --- | --- |
| V11 migration | fresh V11；populated V10→V11；旧 Blueprint receipt/event exact preservation；future version；checksum/name/DDL collision；copy/swap/backup fault rollback |
| Capability authority | Timeline-only、Blueprint-only 与 mixed explicit scopes；wildcard/duplicate/order rejection；wrong DB/project/action；expiry/revoke/exhaust/restart |
| Proof transport | nonce replay；method/path/project/body/key/action substitution；malformed/oversize JSON；proof/secret redaction |
| Timeline transaction | four B2B3 edits；CAS race；same/different idempotency replay；source/Blueprint/Coverage stale；fault after operation/revision/receipt/use insert；cold replay/tamper |
| Handoff security | bootstrap one use/expiry；session expiry；wrong Host/Origin/method/content type；CSP；path/source/secret absence；bounded session/body/history |
| Editor state | pending is noncanonical；Discard；Save then reread；conflict refresh；replace candidate server derivation；reload/process crash before and after commit |
| Host parity | Codex N→N+1 then DeepSeek reads N+1→N+2；reverse journey；new chat/no transcript；exact URL button/fallback |
| Legacy honesty | old editor remains Unsaved Draft Lab；0 canonical mutations/receipts；no import into canonical handoff |
| Electron boundary | pairing approval/revoke works；no Electron Timeline-workbench interaction required for edit/save journey |
| Repository gate | Core/plugin schema parity；Python warnings-as-errors；plugin tests；Node/renderer/typecheck/build；diff check；final repository gate |

## Out of scope

- 把 legacy editor 的 split/delete/transition/crop/fit/volume/canvas/undo/redo 等完整方言直接升级为 canonical 命令。
- 字幕、source audio、配乐、旁白、混音、多轨、final-fidelity preview 和 render/export 能力。
- B2C 跨资源 unified history、通用 undo/redo/branch 和对 Agent/UI 交错操作的完整历史界面；B2C0 只新增 Timeline-local read-only history 与 append-only restore foundation。
- 从 legacy process-local draft 自动迁移或推断 canonical Timeline。
- 通用 write MCP、远程 editor server、跨 OS 用户 credential 隔离、已验 Codex/DeepSeek 厂商身份。
- 移除 Electron 打包/runtime 管理。B2B4 只改变主编辑入口，不宣称整个 App shell 已可删除。
- Remote CI、release/tag、签名、公证、多机同步或网络协作。

## Promotion rule

当前可写成 `IMPLEMENTED IN SHARED WORKTREE / PROMOTION BLOCKED`。只有在以下证据同时存在时，才可升级为 `LOCAL VALIDATION WITH RESIDUALS`：

1. V10→V11 populated migration 与 Core/plugin exact schema parity 通过，旧 Blueprint paired history 逐条可验。
2. paired `timeline.apply_edit` 的 capability/proof/CAS/idempotency/atomicity/cold-audit/tamper 矩阵通过。
3. Codex 和 DeepSeek 的 canonical editor handoff 共用同一实现，安全负向测试通过。
4. 两个方向的 fresh cross-host journey 都证明同一 `database_uuid/project_id/head` 按 N→N+1→N+2 前进，无聊天传递。
5. legacy editor 保持 Unsaved Draft Lab，Electron 仅作 pairing/runtime 边界，项目写没有第二真源。
6. focused gates 和 final repository gate 以新鲜证据通过。

即使 B2B4 本地验证通过，没有 B2C unified history、final-fidelity preview、audio/subtitle 和 fresh export journey 时，ML-015/V3 仍不得标记为完成。
