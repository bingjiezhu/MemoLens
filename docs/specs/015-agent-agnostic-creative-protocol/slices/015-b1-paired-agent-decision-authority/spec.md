# Feature Specification: Paired Agent Proposal & Decision Authority

- Feature ID：`ML-015-B1`
- 创建日期：2026-08-22
- 状态：`IMPLEMENTED / VALIDATED`
- 实施授权：用户已授权继续按 Grill Me 共识逐片实现
- 父规范：[ML-015 Agent-Agnostic Creative Protocol](../../spec.md)
- 前置切片：[ML-015-B0 Canonical Blueprint Proposal Ledger](../015-b0-canonical-blueprint-proposal-ledger/spec.md)
- 产品决策：[43 问产品对齐记录](../../../product-decisions-43-questions-2026-08-22.md)
- 相关安全边界：[ML-007 Local Capability Boundary](../../../007-local-capability-boundary/spec.md)
- 优先级：P0；先让任意 Agent 安全参与同一项目，再把“提案来源”和“用户语义确认”严格分开

## Overview

B0 已经建立了不可变、可恢复、但永远不冒充用户确认的 Creative Blueprint proposal 真源。B1 补上两个相互独立的闭环：

```text
外部 Agent
  → 发起现有项目的短期 pairing
  → Electron main 原生核对项目、权限、短码、时限
  → scoped capability
  → 每条命令使用一次性 nonce + exact request proof
  → Core 复用 B0 CAS / proof / receipt 写入 unverified proposal

当前 Blueprint head
  → Electron main 从 Core 获取 exact decision presentation
  → 用户原生确认或撤销 1–8 个 decision unit
  → append-only authority event / head / receipt
  → 独立 authority projection
```

pairing 只表示“允许这个本地 Agent 会话在指定项目里提交短期、可撤销的 proposal”；它不表示用户同意了立场、脚本、方向、技巧或发布。semantic confirm/revoke 只属于用户，并且只能由 Electron main 持有的独立 authority credential 在原生手势之后提交。

B1 不强制用户在每个创作步骤停下来确认。用户可以让 Agent 连续形成完整 proposal，也可以只确认自己已经决定的 unit；8/8 confirmed 也不等于 planning ready、可生成 Timeline、可渲染或同意发布。

## Product Decisions

### Two authority domains, never conflated

1. **Project-write authority**：短期 pairing 授予现有项目内 `blueprint.commit_proposal` 与 `blueprint.restore_revision`，effect class 仍是 `reversible_project_write`。
2. **Semantic user authority**：用户对固定 decision unit 的内容摘要作 confirm/revoke；Agent、renderer boolean、聊天文本、desktop session token 都不能产生它。
3. persisted Blueprint/v1 内的 creation-time `authority.state=unverified` 永不改写。B1 只在读取投影中增加 `creation_authority` 与 `authority_projection`。

### Pair once, edit fluently

- 一次原生 pairing 后，合法 Agent 在短时窗口内可连续提交多次 proposal/restore，不为每次普通可撤销写弹窗。
- 默认 TTL 为 15 分钟，硬上限 30 分钟；默认/硬上限 operation 数分别为 20/32。
- scope 必须精确绑定 `database_uuid + runtime_authority_epoch + project_id + paired_subject_id + explicit actions + expiry + max_operations`；没有 wildcard。
- 每条命令仍使用 server nonce 与 HMAC proof，绑定 capability、nonce、HTTP method、canonical path、database UUID、project、canonical body digest 与 Idempotency-Key。
- pairing secret 由 CLI 生成，只在首次 loopback pairing intake 交给 Core 内存；DB 只保存 SHA-256。CLI 使用受限 app-state credential file（目录 `0700`、文件 `0600`）内部管理，不进入 argv、env、stdout、日志、MCP result 或项目数据库。
- 当前 OS 用户账户和能直接读取该用户全部文件的进程仍属于 TCB；B1 不宣称跨同一 OS 用户的秘密隔离。backend 重启、authority epoch 变化、过期或原生撤销都会使 capability 失效。
- Agent 自报的 Codex/Claude/vendor/model 名称只是 `claimed_client_label`；可验证身份仅是 proof-secret possession，不得显示成已验证厂商身份。

### Decision-unit authority

沿用 B0 已冻结的 8 个单位和顺序：

1. `intent_goal`
2. `intent_stance`
3. `script`
4. `creative_direction`
5. `output`
6. `material_constraints`
7. `references`
8. `techniques`

“确认整版”只是一次原子选择这 8 个 unit，包括内容为空的 unit；不存在不可解释的 `blueprint_confirmed=true`。

每个 confirm 必须绑定 current exact head、unit 名称、unit semantic digest、presentation contract/digest、native gesture nonce、authority operation 和时间。revoke 还必须绑定当前 active confirmation；它只撤销 authority，不修改 Blueprint semantic，不删除旧事件。

### Carry-forward and restore

- 普通新 revision 只沿直接父链延续 digest 完全相同的 active unit confirmation。
- 某 unit 一旦相对直接父版改变，该 unit 的连续确认链立即中断；以后即使内容摘要再次相同，也不会自动复活更早的 confirmation。
- restore 只恢复 semantic。它只可保留“相对 restore 前直接父版没有改变”的 unit authority，不从 restore source 携带历史 authority。
- 再次授权必须生成新的 native confirm event；不提供 restore authority pointer。
- exact historical revision 的 projection 是该 revision 时点的 `as_of_revision` 视图；current 读取始终报告 current head 的 authority。

## User Scenarios & Testing

### User Story 1：一次配对后连续提交可撤销提案（Priority: P1）

**Given** 已有有效 Blueprint head 的项目，**When** Agent 发起 pairing，用户在 Electron main 原生窗口核对项目、head、权限、短码、TTL 与最大操作数并允许，**Then** Agent 可在窗口内连续提交 proposal/restore，结果全部仍为 user-authority-unverified。

**Independent Test**：只批准一次 pairing，连续提交十次 exact-CAS proposal/restore；只出现一次 pairing 手势，每次 operation 都诚实记录 paired Agent，用户确认数保持不变。

### User Story 2：未配对表面不能写（Priority: P1）

**Given** 调用者只有 loopback、Origin、desktop token、`MEMOLENS_PLUGIN_TRUST_LOCAL_API=1`、伪造 client label 或聊天中的“用户同意”，**When** 它尝试 Agent commit/restore、approve、revoke 或 semantic confirm，**Then** Core 在 mutation 前拒绝，所有 ledger cardinality 不变。

### User Story 3：用户只确认已经决定的内容（Priority: P1）

**Given** current head 有 8 个 unit，**When** 用户在原生 review 中选择 stance、script 与 direction，**Then** 一次事务写入 batch authority event/head/receipt；这三项 confirmed，其余五项 unverified。

### User Story 4：只使发生变化的决定失效（Priority: P1）

**Given** script 与 output 已确认，**When** paired Agent 只修改 script，**Then** 新 head 的 script 变为 unverified，output 从直接父链延续；Agent 请求不能传入或覆盖该 projection。

### User Story 5：撤销和恢复不伪造用户意图（Priority: P1）

**Given** 当前 script 已确认，**When** 用户撤销后 Agent 恢复一个历史 revision，**Then** revoke 不改 semantic；restore 创建新 proposal revision，但历史 script confirmation 不复活。

### User Story 6：跨 Agent 准确恢复 authority（Priority: P1）

**Given** Codex 提交 proposal、用户只确认 3 个 unit，**When** Claude 在没有旧聊天的新会话中 `blueprint-get`，**Then** 它准确看到 current head、3/8 confirmed、各 unit 状态、creation authority 与 remaining gaps，不把项目称为已定稿。

## Contract

### Main-only authority channel

- Electron 每次启动生成独立 `mainAuthorityToken` 与 `runtimeAuthorityEpoch`，随 managed backend spawn 通过 `MEMOLENS_MAIN_AUTHORITY_TOKEN`、`MEMOLENS_RUNTIME_AUTHORITY_EPOCH` 注入。
- main 使用 `X-MemoLens-Main-Authority` 进行 originless direct fetch；该 header 不进入 renderer webRequest 注入、preload、CORS allowlist、日志或响应。
- desktop session token 继续只表示 trusted renderer session。持有它不能 approve/revoke pairing，也不能 confirm/revoke semantic authority。
- main 在每个原生动作前重新验证 backend identity；main 自己向 Core读取 pending/presentation，不能信 renderer 传入的 title、scope、head、digest、summary 或 `confirmed=true`。

### Pairing and command proof

Pairing intake 是 closed-world、loopback、originless、bounded request；它只创建内存 pending，不改变项目。pending 绑定 exact observed head，原生批准时 Core 在同一 DB transaction 再校验 database、project、head、expiry、pending digest 与 presentation digest。

批准后形成 immutable capability facts、native pairing receipt 与 append-only issued event。pending secret 保留在当前 runtime 内存；DB 只保存 secret hash。每个 Agent write 先获取短期 single-use nonce，再计算：

```text
HMAC-SHA256(secret,
  "MLCAP1\n" + capability_id + "\n" + nonce + "\n" + method + "\n" +
  canonical_path + "\n" + database_uuid + "\n" + project_id + "\n" +
  canonical_body_sha256 + "\n" + idempotency_key)
```

Core 在 B0 `BEGIN IMMEDIATE` 内完成 definitive capability check、scope/epoch/expiry/max-use/revocation check、receipt replay/conflict、use event、Blueprint operation/revision/head 与 command receipt。route 层的 HMAC/nonce检查只是 transport proof，不是最终授权。

已完成 command 的相同 receipt identity 在 capability 仍有效时返回冻结响应且不重复计数；显式 revoke、epoch mismatch、backend restart 或 expiry 后不再接受写请求。已完成 revision 不会因 capability 失效而消失。

### Server-derived command context

B1 扩展 B0 operation actor/origin 为封闭 union：

- 旧 desktop row 保持逐字可验证。
- paired Agent row 由 grant 推导 `paired_subject_id`、`capability_id`、claimed label 与 `agent_cli` surface。
- `identity_verified=true` 仅表示本次 paired-session proof 有效；必须同时返回 `vendor_identity_verified=false` 与 `user_authority_verified=false`。

Agent command body 不接受 actor、origin、authority、principal、capability scope、native confirmation、persisted identity 或任意路径字段。

### Authority ledger and projection

V5 以独立 append-only authority ledger 保存 native batch event；它不加入 B0 的 Blueprint-only operation 表，也不冒充 B2 complete project history。authority head 只可按 sequence 前进；event/head/永久 receipt 必须同事务全有或全无。

读取 projection 时 Core：

1. 验证 Blueprint revision chain 与 authority event/head/receipt chain。
2. 重算每个 observed revision 的 8 个 unit digest。
3. 对所选 revision 的每个 unit，只沿“直接父版 digest 连续相同”的区间处理 confirm/revoke。
4. 返回 `unverified | partially_confirmed | confirmed`、8 个 unit 的 active confirmation metadata、confirmed count 与 `as_of_revision`。

输出至少区分：

```json
{
  "creation_authority": {"state": "unverified", "verified": false},
  "authority_projection": {
    "state": "partially_confirmed",
    "confirmed_decision_unit_count": 3,
    "decision_unit_count": 8,
    "as_of_revision": 4,
    "decision_units": {}
  }
}
```

### V5 storage

V5 采用 additive tables，不重写 V4 bytes/checksum：

- `agent_project_capabilities`：immutable grant facts、secret hash、scope、epoch、TTL、max operations、pairing receipt binding。
- `agent_project_capability_events`：append-only `issued | used | revoked`；used event 与 exact Blueprint operation/receipt 绑定。
- `agent_pairing_confirmation_receipts`：原生 pairing presentation/gesture 与 capability 绑定。
- `agent_blueprint_command_receipts`：paired principal 的永久幂等 receipt；B0 desktop receipts 保留在原表。
- `blueprint_decision_authority_events`：confirm/revoke batch、observed head、unit digests、active-confirmation refs 与 presentation proof。
- `blueprint_decision_authority_heads`：显式 authority sequence/head。
- `blueprint_decision_authority_receipts`：main-native authority command 的永久 replay identity。

所有 facts/events/receipts 由 trigger 阻止 UPDATE/DELETE；authority head 只能递增一位。v4→v5 migration 前创建受限、带 hash manifest 的本地 backup；fresh/migration/future/checksum/name-collision/physical-schema corruption 全部 fail closed。

### Surfaces

Agent loopback surface：

- `POST /v1/agent/pairings`
- `GET /v1/agent/pairings/{pairing_id}`
- `POST /v1/agent/capabilities/{capability_id}/nonce`
- `POST /v1/agent/creative/projects/{project_id}/blueprint/commit`
- `POST /v1/agent/creative/projects/{project_id}/blueprint/restore`

Main-only surface：

- `GET /v1/main/agent/pairings`
- `GET /v1/main/agent/pairings/{pairing_id}/presentation`
- `POST /v1/main/agent/pairings/{pairing_id}/approve|reject`
- `GET /v1/main/agent/capabilities`
- `GET /v1/main/agent/capabilities/{capability_id}/revoke/presentation`
- `POST /v1/main/agent/capabilities/{capability_id}/revoke`
- `POST /v1/main/creative/projects/{project_id}/blueprint/authority/presentation`
- `POST /v1/main/creative/projects/{project_id}/blueprint/authority/confirm|revoke`

CLI 必选入口：`agent-pair`、`agent-pair-status`、`agent-capability-status`、`blueprint-commit`、`blueprint-restore`。MCP 在 B1 继续保持读取/候选验证；write MCP 不作为 B1 前置，也不能复用 legacy local-API trust 开关。

Desktop 只增加一个最小“Agent 与创作决定”面板：pending pairing、active capability/revoke、current authority 8-unit review。它不重做 B2 工作台/history，也不把 legacy VideoWorkbench 冒充 Blueprint editor。

## Functional Requirements

- **FR-B1-001**：B1 只允许对已有、非 archived、拥有有效 Blueprint head 的项目 pairing；不得创建项目。
- **FR-B1-002**：pairing approval 与 semantic confirm/revoke 必须是独立 authority domain。
- **FR-B1-003**：pairing、capability revoke、confirm、revoke 只可由 main/backend policy owner签发；renderer 和 Agent 不得取得 issuer secret。
- **FR-B1-004**：main authority token 与 desktop session token 必须独立生成、传递、轮换与校验；前者不得暴露给 renderer/CORS/plugin。
- **FR-B1-005**：pairing 必须绑定 database、runtime epoch、project、subject、exact action allowlist、TTL、max operations、secret proof 和 native presentation。
- **FR-B1-006**：每个 Agent write 必须使用 single-use nonce 与 method/path/db/project/body/idempotency-bound HMAC proof。
- **FR-B1-007**：definitive grant check/use 与 B0 mutation/receipt 必须在同一 `BEGIN IMMEDIATE` 事务线性化。
- **FR-B1-008**：Agent proposal/restore 必须复用 B0 schema、strict JSON、reference proof、CAS、atomicity、durable replay 与完整性校验。
- **FR-B1-009**：operation actor/origin 必须由 Core 从 authenticated command context 推导；claimed vendor identity保持未验证，user authority固定 false。
- **FR-B1-010**：旧 desktop operation/receipt 必须保持逐字可验证；paired receipt 使用独立 V5 表且跨表 cardinality 必须恰好一条。
- **FR-B1-011**：confirm/revoke 必须使用固定 8 个 decision unit；选择必须按合同顺序、唯一、1–8 项。
- **FR-B1-012**：整版确认必须原子确认 exact current head 的 8 个 unit，不产生未来 blanket authority。
- **FR-B1-013**：authority 命令必须绑定 exact head、unit digest、presentation digest、native gesture nonce与 main authority epoch；dialog期间 head变化必须 conflict。
- **FR-B1-014**：authority event、head、receipt必须 append-only/CAS/permanent-replay/fail-closed，并与 frozen Blueprint document分离。
- **FR-B1-015**：普通新 revision 只可延续直接父版 digest相同且 active 的 unit；changed unit必须 unverified。
- **FR-B1-016**：restore不得从历史 source恢复 authority，只可保留相对当前直接父版没有改变的 unit authority。
- **FR-B1-017**：revoke只改变 authority，不改 semantic、不删除旧 confirmation；再次授权必须产生新 confirm。
- **FR-B1-018**：Agent不得执行 confirm/revoke；renderer boolean、聊天声明、caller actor/authority字段一律无效或拒绝。
- **FR-B1-019**：API、CLI、plugin snapshot与project resume必须区分 creation authority和current/historical projection。
- **FR-B1-020**：8/8 confirmed不得被解释为 planning ready、open decisions已解决、compiler ready、renderable、publish approved。
- **FR-B1-021**：pairing/credential/capability/main secret不得进入常规输出、日志、receipt、project data或MCP内容；CLI credential落盘必须限制权限并原子写入。
- **FR-B1-022**：revoke、expiry、max-use、runtime restart/epoch变更后不得开始新写；已完成operation继续可读、可恢复。
- **FR-B1-023**：pending/nonce必须只在有界内存broker中存在，具备TTL、数量与速率上限；不得让unauthenticated intake直接移动head。
- **FR-B1-024**：B1不增加模型、外网、媒体读取、FFmpeg、render、export、任意路径或原文件副作用。
- **FR-B1-025**：有Blueprint head时，legacy Timeline create/revise/new-render阻断必须保持；B1不得吸收B2 compiler。
- **FR-B1-026**：Core、HTTP、Electron 与 standalone plugin 必须共享冻结的 authority 资源合同：单个持久 authority/presentation/receipt JSON 值不超过 1 MiB；每项目 authority event 不超过 4096；event JSON 与 receipt response JSON 的项目级聚合预算分别不超过 64 MiB。任何表面不得设置更窄、会拒绝合法 Core 状态的隐含限制。
- **FR-B1-027**：authority 读取必须在 `SELECT *` 或 JSON decode 前检查类型、逐值字节数、行数与聚合预算；revision 与 event/receipt 验证必须流式、有界缓存，SQL statement 数不得随 revision 数线性增长。
- **FR-B1-028**：authority 写入必须先识别并精确返回已经完成的永久 receipt replay，再对新 event/receipt 做 prospective capacity 检查；超限必须在任何 INSERT/UPDATE 前原子拒绝。
- **FR-B1-029**：native capability 列表是当前 runtime 的可操作撤销面，不是历史账本导出；它只返回 current epoch 中仍 active 的 capability。旧 epoch、revoked、expired 与 exhausted grant 继续保存在不可变账本并接受项目完整性验证，但不得挤爆当前操作面。
- **FR-B1-030**：所有 Agent-facing Blueprint、history、project-resume 与 MCP 输出不得使用无作用域的 `user_confirmed` 或 `authority_verified` blanket boolean；必须以 `creation_authority_verified=false`、state、confirmed count 和显式 `any/all decision units confirmed` 语义区分 creation authority 与 revision-scoped decision authority。

## Stable Errors

| Code | HTTP | Meaning |
| --- | ---: | --- |
| `agent_pairing_required` | 401 | 没有可用 pairing/capability |
| `agent_pairing_proof_invalid` | 401 | secret/nonce/HMAC proof 无效 |
| `agent_pairing_pending` | 409 | 等待用户原生处理 |
| `agent_pairing_denied` | 403 | 用户拒绝 |
| `agent_pairing_expired` | 403 | pending/grant 已过期 |
| `agent_pairing_revoked` | 403 | grant 已撤销 |
| `agent_pairing_scope_denied` | 403 | project/action/db 越权 |
| `agent_pairing_rate_limited` | 429 | pending/nonce 速率或数量超限 |
| `agent_operation_nonce_invalid` | 403 | nonce缺失、过期、错scope或已用 |
| `agent_operation_request_mismatch` | 409 | proof 与 method/path/body/key 不匹配 |
| `agent_confirmation_forbidden` | 403 | Agent 尝试 semantic confirm/revoke |
| `native_confirmation_required` | 403 | 缺少 main-owned authority proof |
| `blueprint_confirmation_stale` | 409 | presentation 的 head 已变化 |
| `invalid_blueprint_decision_units` | 422 | unit缺失、重复、乱序或digest错误 |
| `blueprint_confirmation_not_active` | 409 | revoke目标不是active confirmation |
| `blueprint_authority_idempotency_conflict` | 409 | authority key绑定了不同请求 |
| `blueprint_authority_integrity_error` | 409 | authority ledger/head/receipt损坏 |
| `blueprint_authority_capacity_exceeded` | 409 | 新 authority event/receipt 将超过冻结的项目资源预算；未发生部分写入 |

B0 已有的 head/idempotency/reference/integrity错误继续复用，不建立近义第二套错误。

## Non-goals / B2 Boundary

- 新建 Blueprint-only project。
- Blueprint→Coverage/Timeline compiler、解除 legacy Timeline 阻断。
- Blueprint/authority/Timeline/Preview/Export 的 complete unified project ledger。
- 完整 history、compare、undo/redo、branch 或自然语言 multi-operation compiler。
- Wiki generation、Technique Card、research snapshot 新 proof store。
- render、export、素材使用清单、素材包、任何文件管理或发布状态。
- 长期后台 token、远程 pairing、跨设备、多用户或厂商身份认证。
- Agent 从聊天文本自动生成 semantic confirmation。
- write MCP、deep-link approval 或 generic capability issuer。

## Success Criteria

- **SC-B1-001**：loopback、Origin、desktop token、local-API trust、伪造字段任意组合产生的未授权 Agent/authority 写入数为 0。
- **SC-B1-002**：一次 pairing 后至少 10 次合法 proposal/restore 不出现额外原生授权；越权 action 成功数为 0。
- **SC-B1-003**：100 轮 nonce/proof replay、method/path/body/key/project/db substitution、expiry、revocation与 response-loss 中重复 domain mutation 数为 0。
- **SC-B1-004**：pair/confirm/revoke 全部 fault checkpoint 中 grant/event/head/operation/receipt要么全写、要么全不写。
- **SC-B1-005**：随机 200 步 proposal/no-op/confirm/revoke/restore/conflict 状态机中 authority projection 与 reference model 一致率 100%。
- **SC-B1-006**：unchanged-unit confirmation 保留率 100%；changed-unit错误保留和 restore历史复活均为 0。
- **SC-B1-007**：Codex写入后Claude无聊天恢复时，对current head、confirmed/unverified units、creation authority与gaps判断准确率100%。
- **SC-B1-008**：main/capability/proof secret在stdout、stderr、API/MCP response、日志、SQLite receipt与测试快照中的泄漏数为0。
- **SC-B1-009**：V4→V5迁移逐字保留旧Blueprint revision/operation/receipt与database UUID；未知/future/checksum/physical corruption全部fail closed。
- **SC-B1-010**：B1全量回归后 legacy Timeline新写成功数仍为0，旧Timeline read与既有frozen render replay保持可用。
- **SC-B1-011**：合法近上限 Blueprint 在 Core→HTTP→Electron 与 Core→standalone plugin 两条路径完整读取成功；超过逐值、4096行或任一64MiB聚合预算时，在大对象解码或持久写入前100%稳定拒绝。
- **SC-B1-012**：至少10,000个 synthetic restore-heavy revision 的 Agent projection 只保存有界的 revision/document identity，固定缓存不超过64项；Core 与 standalone plugin 的真实 commit-only/restore-heavy 10、50、100 revision 公共读取中，SQL statement 数不随 revision 数增长。
- **SC-B1-013**：数据库累计至少257条旧 runtime capability 后，当前 active capability仍可在native面板列出并撤销；历史行数和字节保持不变。
- **SC-B1-014**：0/8、1/8与8/8读取中，creation authority始终诚实为unverified；1/8只表达`any=true/all=false`，8/8也继续保留open-decision/compiler/render/export/publish边界。

## Implementation Evidence

- V5 以 additive migration 交付，未改写 V1–V4 checksum；Core 与 standalone plugin 的 V5 schema checksum 一致为 `5889c2f79a36aeed8937e4d8f31994767cd56ae193d8bf27dc636906abc72526`。fresh V5、V4→V5 backup/manifest、name collision、future/checksum/physical corruption 均有 fail-closed 回归。
- Core→HTTP→Electron 与 Core→standalone plugin 均接受合法近上限 authority state，并在 decode 或 mutation 前拒绝 BLOB/NULL、单值超限、第 4097 个 event 与任一 64 MiB aggregate 超限；receipt replay 优先于 prospective capacity gate。
- Core 公共 `get_blueprint_authority_projection` 的 commit-only 与 restore-heavy 10/50/100 revision 均为 `30 SELECT / 32 total statements`；standalone plugin 对应两组均为 `151 SELECT / 170 total statements`。10,000 revision restore-heavy plugin oracle 的 tracked-object 峰值为 12，小于 64 的冻结上限。
- Pairing/global pre-auth、nonce/HMAC substitution/replay、capability revoke/expiry/exhaustion/restart、main-only presentation、authority confirm/revoke、fault rollback、receipt/sidecar tamper、secret split-output redaction 和固定 seed 200 步状态机均通过。0/8、1/8、8/8 输出递归拒绝裸 `user_confirmed` / `authority_verified`，MCP `write=false` 保持不变。
- 2026-08-22 仓库级 `npm run check` 退出 0：Ruff、218 项 Core/backend Python、223 项 plugin、local deployment verify、TypeScript/Electron/Vite build、71 项 Node 与 46 项 renderer-model tests 全部通过。
- 独立只读安全/架构复核在稳定 checkout 上复现 Core 常量斜率，验证 missing/future/wrong-digest restore target、no-change BLOB predecode 和 Core/plugin/MCP 边界；最终结论为 `P0=0 / P1=0 / Go`。下列 P2 是已接受的非阻断债务，不得改写为“已解决”。

## Threat Model and Residual Risk

B1 防御 compromised renderer、未配对 loopback client、token confusion、scope substitution、proof replay、stale presentation 与 Agent 自报身份升级。Electron main、当前 backend、SQLite engine 与当前 OS 用户属于 TCB；同一 OS 用户直接替换整个 DB、读取 CLI credential file、调试 main/backend 内存或控制已配对 Agent进程不在密码学隔离承诺内。SQLite immutable trigger和digest提供 fail-closed/tamper-evident，不提供对同一用户整体DB替换的签名防伪。

网络只限 loopback，B1不新增外网调用。proof secret使用普通app-state受限文件而非跨平台系统keychain，是首版CLI兼容取舍；若未来 threat model要求防同用户进程读取，应另立OS keychain/硬件签名客户端身份切片，不能静默扩大本规范承诺。

当前保留以下非阻断 P2：

1. **健康历史每次写入仍全链重验**：单次 commit/restore 的扫描与 decode 工作随 revision 线性增长，连续构建累计为 `O(R²)`。独立本机数据为 commit r10/r50/r100 `0.211/1.131/2.220s`，restore r11/r51/r101 `0.275/1.291/2.699s`。V6/maintenance 应引入绑定 database/schema/head/operation/receipt/capability closure 的 verified-prefix/checkpoint；健康 same-runtime append 目标为 O(1)，restart 可允许一次 O(R) full audit，不得通过关闭历史验证达标。
2. **完整 capability 历史仍为 `O(capabilities + Agent operations)`**：单 capability event 由 `max_operations<=32` 有界，258 条真实历史 capability 的先前独立回放约 65.8 ms / 1840 SQL statements。当前 native 操作面已只列 current epoch active grant；V6 仍需项目级 admission/aggregate budget 或可验证 checkpoint。
3. **近 1 MiB decision content 的原生审阅体验可能退化为大量分页弹窗**：当前每页 1200 code points 且逐页确认。后续应改为 main-owned、可滚动/虚拟化的 exact-content review，仍绑定 digest 与独立最终 gesture。
4. **Electron main authority fetch 未设置超时**：local backend 不返回 header/body 时 review 可无限等待。后续应冻结 10–15 秒 `AbortSignal` 和 never-resolving fetch/no-mutation 回归。
5. **CLI credential 读取存在路径 TOCTOU，且 16 KiB 上限在全量读取后才检查**：当前同 OS 用户在 TCB 内，所以不阻断 B1；后续应使用 `os.open(O_NOFOLLOW)` + `fstat` + 最多 16385-byte bounded read。
6. **MCP 部分旧 Blueprint 内层 output schema 仍比 runtime frozen validator 宽**：当前真实输出均经严格 validator，不构成兼容或越权错误；后续应复用 expanded Blueprint schema/$defs，防止未来意外字段被 output schema 宽松接受。

另外，plugin exact-history 最多按固定返回窗口读取 100 个 bundle，与完整 revision 总数无关；Core 已冻结 desktop-authority 历史的公共斜率 oracle，agent-capability-heavy 历史仍应在上述 V6 性能切片补同等 10/50/100/1000 oracle。在这些切片交付前，不得宣称 B1 的长期历史或原生审阅规模无界可扩展。
