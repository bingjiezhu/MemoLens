# Feature Specification: Canonical Blueprint Proposal Ledger

- Feature ID：`ML-015-B0`
- 创建日期：2026-08-22
- 状态：`IMPLEMENTED / VALIDATED`
- 实施授权：用户已授权继续按 Grill Me 共识逐片实现；本切片只建立已有项目的 Blueprint proposal 真源与可恢复历史
- 父规范：[ML-015 Agent-Agnostic Creative Protocol](../../spec.md)
- 前置切片：[ML-015-A1 Creative Blueprint Shadow Contract](../015-a1-creative-blueprint-shadow-contract/spec.md)
- 产品决策：[43 问产品对齐记录](../../../product-decisions-43-questions-2026-08-22.md)
- 优先级：P0，先建立诚实、可恢复、无双真源的项目内核，再开放 Agent 写入或用户确认

## Overview

ML-015-A1 的 candidate 是协商和预检 envelope，不是项目状态。B0 第一次让 Creative Blueprint 成为已有项目的持久真源，但只把它定义成可撤销的 **未确认提案**：显式 head 决定项目当前依据哪一版；任何字段仍不能因此被描述成用户已同意。

```text
A1-valid semantic proposal + B0-resolvable reference pins
  → strict proposal command
  → Core compile + same-transaction proof
  → immutable Blueprint revision
  → explicit project Blueprint head
  → append-only Blueprint operation
  → durable command receipt

任意历史 revision
  → restore command
  → 新 revision + 新 operation
  → 历史不删除、不改写
```

B0 只接管已经存在于 `creative_projects` 的项目，不创建新项目、不实现用户确认、不接 UI、不生成素材 coverage 或 Timeline。CLI/MCP 只新增读取能力；生产写入仍只存在于 desktop-authenticated Core API，不能通过 `MEMOLENS_PLUGIN_TRUST_LOCAL_API=1` 或 Agent 自报身份获得。直接改 SQLite 不属于受支持写入协议，也不能产生用户确认语义；当前完整性保证是结构与摘要层面的 fail-closed/tamper-evident，不是对同一 OS 用户替换整个数据库的密码学防伪。

## Product Decision

### Canonical state is not semantic approval

- `creative_blueprint_heads` 提供 canonical/persisted authority：系统当前使用哪一版 Blueprint。
- `authority.state=unverified` 表示 semantic user authority：没有可信证据证明用户确认了其中的立场、脚本或技巧。
- B0 中所有 revision 都固定为 `status=proposal`、`authority.state=unverified`；desktop session token 只授权可撤销项目写，不是用户确认凭证。
- `caller_declared_user_input`、聊天里的“用户同意了”、技巧 `selected`、请求中的 actor/origin/authority 字段都不能升级 authority。

### One truth, no silent legacy fallback

- 没有 Blueprint head 时，legacy brief 和 A1 shadow 继续作为迁移来源。
- 存在完整且验证通过的 Blueprint head 时，`project-open` 必须优先报告它；`blueprint-shadow` 仍保持 legacy/non-authoritative，不冒充 persisted state。
- head/revision/digest/operation 任一损坏时，读取必须 fail closed；不得使用 `MAX(revision)`、较旧 Blueprint 或 legacy brief 假装 current。
- 项目已有 Blueprint head、但尚无 Blueprint→Timeline compiler 时，legacy `create_from_project` 必须拒绝创建新 Timeline；已有 legacy Timeline 只可作为历史上下文读取，不得 revision 或创建新 render job。已冻结成功响应的相同 render idempotency identity 仍可精确重放，但不得创建新 job。
- 旧 Video Workbench 尚不能解释 persisted Blueprint；项目存在有效 head 时，legacy project GET 必须返回 `creative_blueprint_workbench_unavailable` 并指向 Blueprint read surface，不能继续把 legacy brief 显示成当前创作内容。即使 legacy brief 后续缺失，canonical Blueprint 项目也不得被错报为不存在。

## User Scenarios & Testing

### User Story 1：已有项目采用第一版真正 Blueprint（Priority: P1）

**Given** 已有 project 的 latest legacy brief 完整且 digest 正确，**When** desktop-authenticated Core 收到 closed-world proposal command 与 `expected_head=null`，**Then** Core 在同一事务验证 exact legacy base、编译独立 persisted Blueprint/v1，并写入 revision 1、显式 head、operation 与永久 command receipt。

**Independent Test**：准备 v4 project + legacy brief，不创建 Timeline；提交一次，验证四类记录全有或全无、JSON/row identity 与 digest 一致，candidate object/base/declared source 不进入 persisted document。

### User Story 2：并发修订不静默覆盖（Priority: P1）

**Given** current head 为 revision N，**When** 两个不同 idempotency key 都基于同一 `{revision,content_sha256}` 提交新 proposal，**Then** 最多一个生成 N+1；另一个得到稳定 revision conflict 与 current head metadata，不自动 rebase、merge 或 last-write-wins。

**Independent Test**：并发执行至少 20 次双写竞争；每轮只产生一个新 revision/head advance，失败请求不留下 operation/receipt。

### User Story 3：丢失响应可以永久重放（Priority: P1）

**Given** command 已提交但客户端没有收到响应，**When** 它在 24 小时之后、App 重启后或 head 已前进后以相同 key 和 normalized request digest 重试，**Then** Core 在读取 current head 之前返回首次冻结结果；相同 key 不同 digest 永久 conflict。

**Independent Test**：首次提交后手工推进 head、改变 legacy/reference current state并模拟未来时间；同请求仍精确重放，revision/operation 数不增加。

### User Story 4：任意旧版可以通过新操作恢复（Priority: P1）

**Given** 项目当前为 revision N，**When** 调用者以 exact current head 恢复 revision K，**Then** Core 验证 K 的完整性并创建 revision N+1；新 revision 的 semantic digest 等于 K，parent 仍指向 N，operation 记录 `restore_from`，旧历史保持不变。

**Independent Test**：创建三版后恢复第一版，再重试、冲突和再次恢复；没有 UPDATE/DELETE immutable rows，head 始终显式且可重放。

### User Story 5：新 Agent 可以读取当前提案而无需旧聊天（Priority: P1）

**Given** persisted Blueprint head 有效，**When** 新 Agent 通过 safe-default CLI/MCP 调用 `blueprint-get` 或 `blueprint-history`，**Then** 它从一个私有只读 SQLite snapshot 得到 current/exact revision、语义内容、authority projection、open decisions 与有界操作摘要；工具不写 DB、不联网、不读取原媒体。

**Independent Test**：CLI、MCP、Gateway 对 current 与 exact historical revision 返回等价内容；破坏 current head 后三个入口都 fail closed 且不回退。

## Contract

### Persisted Blueprint/v1

仓库包含独立 schema 工件 `creative-blueprint-v1.schema.json`。它与 A1 candidate/v1 是两个 object、两个 version lifecycle、两个 exact-byte digest；不得通过重命名 candidate 或只添加 revision 字段来持久化。

根对象固定为：

- `object = "memolens.creative_blueprint"`
- `schema_version = "1"`
- row-bound identity：`project_id`、`revision`、`parent`、`created_by_operation_id`
- `status = "proposal"`
- `semantic`：intent、script、direction、output、constraints、material hints、references、techniques、bindings、assumptions、missing evidence、open decisions；不含 candidate `base` 或任何 `declared_source`
- `lineage`：compiler contract、可空 caller-claimed candidate digest、exact initial legacy brief binding
- `evidence_manifest`：仅记录稳定 asset/span proof 或 unresolved 状态，不含路径、provider payload 或原媒体内容
- `authority`：固定 `state=unverified`，每个 decision unit 带 semantic sub-digest、`claim=agent_proposal`、`verified=false`

Digest 语义：

- `schema_sha256`：persisted schema artifact exact bytes。
- `semantic_sha256`：仅 `semantic` canonical JSON 的 SHA-256；恢复同一语义时保持相同。
- `content_sha256`：完整 persisted Blueprint document canonical JSON 的 SHA-256；包含 revision/parent/operation/lineage/authority，因此每个新 revision 独立。
- candidate digest 只作 unverified lineage，不是 revision digest、签名或 authority proof。

B0 保留完整 semantic vocabulary，但写入能力按当前 Core 可证明的引用收窄：asset/span 进入 `evidence_manifest`；project reference 在同一事务验证目标项目存在；Creator Memory binding 只接受数据库级 `profile_id=default` 的 exact revision/content digest，并复用 canonical reader 验证 `user_edit | confirmed_suggestion | reset` 来源语义、profile/evidence、前序 revision 与 brief evidence。非 default、缺失、错配或损坏全部拒绝。`external_https` 和 `user_text` 只作为明确的 untrusted 声明。当前没有 Core 真源的 research snapshot、Technique Card revision 和 Wiki generation 不得伪装成已 pin，B0 commit 会拒绝其非空值；它们在 B2/ML-016 取得可验证 store 或 resolution manifest 后再开放。A1 shadow/validator 仍可表达这些候选，用于协商而不是持久化承诺。

Authority decision units 固定为：`intent_goal`、`intent_stance`、`script`、`creative_direction`、`output`、`material_constraints`、`references`、`techniques`。B0 不接受 verified state；空内容也有确定性 unit digest。

### Typed commands

B0 只有两个领域命令：

1. `blueprint.commit_proposal.v1`
   - body 只接受 `expected_head`、`initial_legacy_brief`、`source_candidate_sha256`、`semantic`。
   - 第一次提交要求 `expected_head=null` 和 exact latest legacy brief；后续提交要求 exact current head，legacy binding由 current revision 继承。
   - Core 计算 section-level typed diff；请求不能传 persisted identity、actor、origin、authority、operation 或 content digest。
   - semantic 完全相同时创建 `no_change` operation 和 durable receipt，但不增加 revision、不移动 head。
2. `blueprint.restore_revision.v1`
   - body 只接受 exact `expected_head` 与 exact `restore_from={revision,content_sha256}`。
   - Core 从已验证 immutable revision 复制 semantic/lineage，再编译新的 unverified proposal revision。
   - restore 不是 head pointer 回拨，也不是 user confirmation。

所有 command 的 normalized digest 在 strict parse、closed-world schema、默认值归一化之后计算；JSON key order、whitespace 和等价 escaping 不改变 identity。

### Durable operation and receipt

每次成功 mutation 在诚实命名的 `creative_blueprint_operations` 形成 append-only记录，至少包含 Blueprint sequence、parent operation、coverage scope、command/effect class、server-derived actor/origin、intent、precondition、typed diff、input/output digest、result 和 timestamp。B0 明确 `coverage_scope=creative_blueprint`、`complete_project_history=false`，因为 legacy Timeline 和其他 writer 尚未纳入；该表不冒充B2的统一project ledger。

Blueprint 使用独立永久 command receipt，而不是现有 24 小时会删除的 transport idempotency cache。receipt scope 由服务端使用 `{database_uuid, authenticated_principal, project_id, command_type/version, idempotency_key}` 组成；请求不能自填。receipt 与 operation 同寿命，损坏时 fail closed，绝不删除后重做。重放在承认冻结响应之前，必须重新验证 receipt 绑定、不可变 operation prefix 的 parent/sequence/state transition，以及关联 revision chain；“replay first”只跳过易变的 current head precondition 与 live reference proof，不跳过不可变历史完整性。

### Read surfaces

- Backend：`GET /v1/creative/projects/{project_id}/blueprint[?revision=N]`
- Backend：`GET /v1/creative/projects/{project_id}/blueprint/operations?limit=N`
- Desktop-only write：`POST .../blueprint/commit`、`POST .../blueprint/restore`
- CLI：`blueprint-get`、`blueprint-history`
- MCP：`memolens_blueprint_get`、`memolens_blueprint_history`

CLI/MCP 不增加 write tool；status 继续 `write_blueprint=false`，并分别广告 persisted read 与 Blueprint-only ledger capability。`MEMOLENS_PLUGIN_TRUST_LOCAL_API=1` 仍只是已有 read-risk opt-in，不能发现、复制或复用 Electron session token。

## Functional Requirements

- **FR-B0-001**：V4 必须新增独立 revisions、explicit heads、append-only operations 和 durable command receipts；不得改变 V2/V3 migration bytes 或 checksum。
- **FR-B0-002**：migration 前必须 fail closed 拒绝 future schema/migration，验证全部已知 checksum；v3→v4 在本地生成 SQLite backup + manifest，再以单事务 additive migration完成。
- **FR-B0-003**：V4 必须阻止 `database_meta.schema_version` 降级，使旧 v3 binary 的正常初始化无法把 v4 DB 标回 3；新版 meta 更新只能单调前进。
- **FR-B0-004**：revisions、operations、receipts 必须由 SQLite trigger 阻止 UPDATE/DELETE；head 是唯一可 CAS 更新的 Blueprint current pointer。
- **FR-B0-005**：第一次 proposal commit 必须绑定 exact existing project 与 latest legacy brief revision/digest；不自动 backfill，不从 shadow 自动提升。
- **FR-B0-006**：persisted Blueprint/v1 必须由 Core 编译，删除 candidate identity/base/declared source，并通过独立 frozen schema、领域不变量和 row/document identity 校验。
- **FR-B0-007**：所有 persisted semantic field 使用 A1 已冻结的大小、ID、引用、控制字符与 cross-reference上限；request raw/canonical/depth/nodes/container/string/integer ceiling 与 A1 相等或更严格。
- **FR-B0-008**：reference proof 必须在同一 mutation connection 完成。asset/span 可形成 verified/unresolved evidence manifest；project 必须验证存在；Creator binding 只接受 `profile_id=default` 的 exact revision/digest，并通过 canonical confirmed-profile reader 验证来源、evidence、predecessor 与 brief evidence；外部 URL/用户文本保持 untrusted 声明；没有 Core proof store 的 research snapshot、Technique/Wiki pin 必须拒绝。不存在/损坏/stale legacy base、head、Creator pin 或 persisted schema 必须阻止写入。
- **FR-B0-009**：每次 revision mutation、head CAS、project timestamp、operation、durable receipt 和冻结 response 必须在同一 `BEGIN IMMEDIATE` 中全有或全无。
- **FR-B0-010**：CAS 必须比较 expected revision 与 content digest；initial null/non-null、revision-only/digest-only、stale/current 全部有确定性语义，不自动 merge/rebase。
- **FR-B0-011**：durable replay 必须发生在 current head、legacy/reference live state 等易变检查之前；同 scope/key/digest 返回首次结果，同 key 不同 digest 永久 conflict。承认 replay 前仍必须验证永久 receipt、完整 operation chain structure、截至该 command 的 operation state prefix 与关联 revision history；不可因 replay-first 绕过不可变账本损坏。
- **FR-B0-012**：普通 proposal 与 restore 不得创建任何 verified authority；请求出现 actor/origin/authority/user-confirmed/persisted identity 等字段必须 closed-world 拒绝。
- **FR-B0-013**：GET/current 只可通过 explicit head join exact revision+digest；head 损坏、缺失但仍有 revision/operation/receipt、JSON 损坏、schema/digest/operation identity 冲突都必须 fail closed，不得回退。
- **FR-B0-014**：plugin 读取使用一个私有只读 snapshot，不联网、不访问 Library/原媒体、不写 source DB/WAL/SHM；history不返回脚本全文、locator、token、key、DB path或 raw operation JSON。
- **FR-B0-015**：有有效 Blueprint head 时，project-open 的顶层 selection 必须声明 explicit Blueprint head 是技术上的 `authoritative_project_head=true`，优先显示 Blueprint authority/open decisions，并把 `creative_blueprint_unavailable` 替换为 `blueprint_user_authority_unverified`；这不等于用户确认。legacy Timeline selection 仅作历史上下文，完整 project ledger、Timeline compiler 与 Wiki generation gap 仍保持诚实。
- **FR-B0-016**：有 Blueprint head 的项目在 B0 不得继续用 legacy brief 创建新 Timeline，稳定返回 `blueprint_timeline_compiler_unavailable`；已有 legacy Timeline 只读，revision 与新 render 稳定返回 `blueprint_legacy_timeline_write_unavailable`，不得伪称绑定 current Blueprint。只有此前已成功冻结的相同 render idempotency identity 可精确重放，且不得创建新 job。
- **FR-B0-017**：B0 不新增模型、网络、shell、subprocess、FFmpeg、任意路径、媒体扫描、Creator Memory写入、原文件移动/复制/删除或 export副作用。
- **FR-B0-018**：B0 不创建新 creative project；没有 legacy brief 的 Blueprint-only project与真正 Agent write pairing放到后续切片。
- **FR-B0-019**：有有效 Blueprint head 时，尚未支持 Blueprint 的 legacy project/workbench read 必须返回稳定 `creative_blueprint_workbench_unavailable` 与 canonical head 指引；不得让 UI 把 legacy brief 当 current，也不得因 legacy brief 缺失把 canonical 项目错报为不存在。

## Non-goals

- user-authored/user-confirmed authority、confirmation receipt、撤销确认或“用户刚才说过”的聊天证明。
- desktop pairing UI、Agent project-write capability、CLI/MCP write tool。
- Blueprint→Coverage/Timeline compiler、剪辑工作台、Timeline统一 ledger。
- branch/merge/CRDT、多人实时协作或任意 JSON Patch。
- 新项目创建、legacy brief删除/改名/双写或自动批量迁移。
- Craft Wiki card执行或持久pin、Wiki generation pin、research snapshot pin、外部参考抓取、模型调用、渲染、导出、素材使用清单或完整素材包。

## Success Criteria

- **SC-B0-001**：fresh v4 与 v3→v4 migration 100% 保留 database UUID/legacy rows；已知/future/checksum/downgrade故障全部 fail closed。
- **SC-B0-002**：所有 fault-injection checkpoint 中 revision/head/operation/receipt数量要么全增、要么全不变；partial state 为 0。
- **SC-B0-003**：同 head 并发写 100 轮静默覆盖为 0；每轮最多一个 revision advance。
- **SC-B0-004**：提交后任意时间重放相同 receipt identity，response digest 与首次一致，domain mutation额外次数为 0。
- **SC-B0-005**：随机 200 步 commit/no-op/restore/replay/conflict 后，explicit head、revision chain、operation sequence、semantic/content digest验证一致率 100%。
- **SC-B0-006**：所有成功 Blueprint 的 authority verified count 为 0；authority注入测试的持久副作用为 0。
- **SC-B0-007**：Gateway/CLI/MCP current/exact/history parity 100%；current损坏回退次数为 0。
- **SC-B0-008**：plugin/backend/Node/renderer/desktop全量回归通过，现有 A0/A1/Wiki/Timeline read合同不退化。

## Implementation Evidence

- persisted Blueprint schema 的 app/plugin 工件保持 exact-byte parity，digest 为 `e239655e84014be04434eae19497706b28e62280b86b9db93bb1c80e32a56d52`。
- 同 head 并发压力为 100 轮；固定 seed 的随机状态机执行 200 步，覆盖 commit/no-op/replay/conflict/restore，并验证 Core→Gateway/CLI/MCP current/exact/history parity。
- commit/no-change/restore 的 operation、revision、head、project timestamp、receipt checkpoint 均有注入失败回滚测试；24 小时后且 legacy/live reference 已删除的永久 replay 仍精确返回。
- legacy Workbench、Timeline create/revise/new render 的 canonical-priority 与稳定错误码均有回归；旧 read 与已冻结 render replay 保留。
- 最终 `npm run check` 通过：Ruff、178 项 backend/Core Python、180 项 plugin、local deployment verify、renderer/Electron build、51 项 Node 与 43 项 renderer-model；独立架构复审另跑 75 项 B0 focused tests。架构与安全复审均无未解决 P0/P1/P2。

## Residual Threat-Model Boundaries

- SQLite 文件、进程账号与本机 runtime 当前属于可信计算基。摘要、foreign key、schema fingerprint、append-only trigger 与全链验证可发现非一致性篡改，但没有用受保护密钥对数据库做签名/MAC；同一 OS 用户任意替换整个一致数据库不在 B0 的密码学防伪承诺内。
- SQLite WAL 当前使用 `synchronous=NORMAL`，覆盖正常进程崩溃、App 重启与永久应用语义。若以后把突然断电后的最高等级 durability 纳入“永久”定义，需要单独评估 `FULL`/`fullfsync`、性能与平台差异。

## Follow-up Status

- `ML-015-B1`：已由 [ML-015-B1 / MemoLens 0.10.0](../015-b1-paired-agent-decision-authority/spec.md) 交付 main-owned desktop pairing、scoped reversible project-write capability、native user gesture 与 decision-unit confirm/revoke。
- `ML-015-B2`：把B0专用 `creative_blueprint_operations` 迁移/投影进Blueprint、Coverage、Timeline、Preview与工作台统一 operation chain；Agent/UI同 head、compare/undo/redo/branch。B2同时为高频交互引入可验证checkpoint/增量审计，B0继续以全链fail-closed正确性优先。
- `ML-018`：script coverage 与全片素材 assignment。
