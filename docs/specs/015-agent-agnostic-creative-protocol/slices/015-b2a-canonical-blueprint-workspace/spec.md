# Feature Specification: Canonical Blueprint Workspace & Honest History Bridge

- Feature ID：`ML-015-B2A`
- 创建日期：2026-08-23
- Spec 状态：`FROZEN / IMPLEMENTATION RECORDED`
- 实施状态：`IMPLEMENTED`
- 验证状态：`LOCAL VALIDATION WITH RESIDUALS`
- Remote CI：`NOT RUN`
- 实施授权：用户已授权按 Grill Me 已达成的产品共识继续实现；本切片仅开放 canonical Blueprint 工作台、诚实历史投影和 CAS restore
- 父规范：[ML-015 Agent-Agnostic Creative Protocol](../../spec.md)
- 前置切片：[ML-015-B1A Authority Runtime Hardening](../015-b1a-authority-runtime-hardening/spec.md)
- 产品决策：[43 问产品对齐记录](../../../product-decisions-43-questions-2026-08-22.md)
- 实施计划：[plan.md](plan.md)
- 任务清单：[tasks.md](tasks.md)
- 实施证据：[implementation-evidence.md](implementation-evidence.md)
- 数据库边界：保持 managed V5 exact schema；无 migration、无新 table/index/trigger/view

## Outcome

B0 让 Creative Blueprint 成为项目的持久语义真源，B1 把用户对 8 个决策单元的确认/撤销与提案写入权分开，B1A 收紧了运行时完整性。B2A 实施前，可视化创作工作台仍只能理解 legacy brief：项目一旦存在有效 Blueprint head，`GET /v1/creative/projects/{project_id}` 会以 `creative_blueprint_workbench_unavailable` 拒绝打开。这个拒绝曾防止 UI 把 legacy brief 冒充为 current，但它也让最真实的项目无法在主工作台恢复。

B2A 只关闭这一个已证实的产品断点：

```text
localStorage 只保存 project_id 提示
  → Core 在一个一致读事务中验证 project + explicit Blueprint head
  → 返回 canonical Blueprint workspace projection
  → UI 分开呈现语义、用户权威、可执行状态和 3 条历史 lane
  → 用户可浏览 exact revision、查看 section diff，并以 exact current head 执行 CAS restore
  → restore 创建新 revision，不回拨 head、不复制 authority
```

这是 **workspace/read-history bridge**，不是 Blueprint→Timeline compiler。有 Blueprint head 时，工作台必须明确显示“尚无与当前 Blueprint 绑定的可执行 Timeline”；任何旧 Timeline 只能进入 legacy artifacts lane，不得被打开为 current canvas、修订、preview 或 render。

## Product and Truth Boundaries

### 1. Semantic truth

- 项目当前的 semantic truth 只来自 `creative_blueprint_heads` 指向的 exact revision/content digest，并必须通过既有 Blueprint revision、operation、receipt、schema 与 digest 完整性验证。
- `authoritative_project_head=true` 只表示 technical canonical selection，不表示用户已确认内容。Blueprint document 仍是 `status=proposal`，creation authority 仍是 `unverified`。
- legacy brief 只是 `migration_context_only`；无论它更新、更完整或 UI 以前已缓存，都不得覆盖、填补或代替 current Blueprint。
- `legacy-only` 精确定义为没有 Blueprint head 且没有任何 canonical Blueprint/authority/capability trace。受支持的 pairing/capability 签发本身要求当时已有 Blueprint head；若这些 trace 仍在但 head 缺失，必须视为 canonical integrity failure，不能回退 legacy。
- 对话 transcript、Agent 自报、renderer state 与 `localStorage` 都不是 semantic truth。

### 2. Executable truth

- B2A 中不存在与 current Blueprint 经可验证 compiler manifest 绑定的 Timeline，因此 `current_executable=null`。
- 工作台必须投影 `state=not_compiled`、稳定 reason `blueprint_timeline_compiler_unavailable`，并明示 Timeline create/edit/preview/render/export 能力为 false。
- legacy Timeline 即使 validation status 为 valid，也只证明它曾对 legacy brief 有效；它不能被推断为 current Blueprint 的 executable truth。
- 只有后续明确授权的 B2B compiler/binding 切片才能把 executable state 从 `not_compiled` 推进为 draft Timeline。

### 3. User decision authority

- 用户决策权只来自 B1 的 decision-authority ledger 对 exact Blueprint revision 的验证投影，不来自 Blueprint creation authority、desktop token、paired Agent capability 或聊天文字。
- 投影必须显示 8 个 decision units 的 confirmed/unverified 状态、confirmed count 与 `as_of_revision`。部分确认就是部分确认，不使用“已定稿”概括。
- 8/8 confirmed 不会自动产生 Timeline、render/export 授权或“已发布”状态。
- restore 复制的是目标 revision 的 semantic content，不复制、迁移或伪造目标当时的用户确认。新 revision 的 authority 必须由现有 Core 失效/投影规则重新计算。
- confirm/revoke 仍只能经 Electron main 原生审阅所有的路径执行；B2A renderer 不新增 semantic-authority write 能力。

### 4. Honest history projection

V5 没有覆盖 Blueprint、authority、legacy brief 和 Timeline 的统一 project ledger。B2A 不通过重命名、时间戳排序或 UI 合并伪造这个能力，而是提供 3 条明确标签的 history lane：

| Lane | 内容 | 可声称范围 | 不可声称 |
| --- | --- | --- | --- |
| `semantic_changes` | Blueprint commit/no-change/restore operation 与 revision metadata | Blueprint 内部 sequence、parent、changed sections、result revision/digest 可验证 | 完整 project history、Timeline 或 authority 事件的全局次序 |
| `decision_authority` | confirm/revoke 事件的安全投影 | 仅 `event_id`、`sequence`、`authority_operation`、`observed_revision`、`decision_units`、`created_at` | native gesture nonce、receipt/token、runtime epoch/secret、path、raw presentation 或将确认冒充内容修改 |
| `legacy_artifacts` | legacy brief revision 与 legacy Timeline revision 的有界 metadata | lane/brief role 固定 `migration_context_only`；Timeline role 固定 `historical_observed_context_only` | authoritative head、current executable、可渲染或与 current Blueprint 相容 |

投影根必须显式返回：

- `complete_project_history=false`
- `global_total_order=false`
- `replay_scope=per_lane_only`
- 每条 lane 的 `coverage_scope`、精确排序键、available count 与截断状态

UI 可以为方便浏览按 `created_at` 将多 lane 项目交错显示，但必须保留 lane 标签并说明这只是 presentation order，不是可重放的全局因果序。

B2A 只返回 latest bounded windows，不提供 cursor 或“继续读取完整历史”的契约：

- `semantic_changes` 按 `sequence_desc` 返回最新 operation 窗口，`available_operation_count` 是总数，窗口必须从总数锚定并连续递减；`available_revision_count` 必须等于 current Blueprint revision。
- `decision_authority` 先选最新窗口，再按 `sequence_asc` 在窗口内呈现；若有事件，最后一条 sequence 必须等于 `available_event_count`，窗口连续递增。
- `legacy_artifacts` 的一个 `history_limit` 由 brief 与 Timeline 共用；brief 按 revision 降序，Timeline 按 created-at 降序、ID 升序、revision 降序，精确 order literal 为 `brief_revision_desc_and_timeline_created_at_desc_id_asc_revision_desc`。这不是 brief 与 Timeline 之间的 total order。
- 完整/可分页 unified history 属于 B2C；B2A 不接受 renderer cursor、任意排序、filter 或 SQL expression。

## User Stories and Independent Acceptance

### US1 — 重新打开 canonical Blueprint 项目（P0）

作为在 Agent 中已经生成或修订 Blueprint 的创作者，我希望重新进入 MemoLens 时直接看到当前主题、文稿、方向、素材提示、未决问题和确认状态，而不是 409 错误或旧 brief。

**Independent Test**：在已存在 valid Blueprint head 且 legacy brief/Timeline 也存在的 fixture 上，仅依靠已保存的 project ID 重启 renderer；工作台显示 exact current Blueprint head 和 authority projection，legacy 内容只出现在标记清楚的历史 lane，不出现为 current brief/Timeline。

### US2 — 一眼分清“已写进项目”、“我已确认”和“已经能剪”（P0）

作为普通创作者，我希望工作台用清楚语言分开提案内容、我已确认的决定和真正可播放的剪辑版本，不被一个模糊的“已完成”标记误导。

**Independent Test**：对 0/8、部分确认、8/8 和后续 Blueprint 变更导致部分确认失效四种 fixture 做截图与语义断言；四者都显示 `not_compiled`，不出现可播放、可 render 或已定稿的误导性 CTA。

### US3 — 比较并恢复任意可见 Blueprint revision（P0）

作为不满意当前创意方向的用户，我希望查看某个历史版本改了哪些区域，对比它与当前版本，然后把它的语义内容恢复成一个新版本，而不删除中间的历史。

**Independent Test**：创建 revision 1–4，选择 revision 1；UI 展示 current→target 的 12-section 差异摘要，POST 使用 exact current `{revision,content_sha256}` 和 exact target `{revision,content_sha256}`，结果为 revision 5。revision 1–4、原 operation 和 authority event 字节不变；revision 5 的 authority 按 Core 规则重新投影，不复制 revision 1 当时的确认。

### US4 — 并发修改不被 restore 覆盖（P0）

作为同时在 Agent 与工作台修改项目的用户，我希望 restore 只在我刚才审阅的 head 仍是 current 时成功。

**Independent Test**：UI 打开 revision 4 并准备恢复 revision 1，然后 Agent 先提交 revision 5；UI 的 restore 必须稳定返回 head conflict，不自动重试、不产生 revision 6，并要求重新获取/比较 current head。

### US5 — 历史可用，但不伪造统一 ledger（P1）

作为回看项目的创作者，我希望同时看到内容修订、用户确认/撤销和旧制品，也希望软件诚实告诉我它们目前不是一条可全局 replay 的历史。

**Independent Test**：三条 lane 各有至少两个事件，其 `created_at` 交错；UI 保留 lane/role 标签，API 报告 `complete_project_history=false` 与 `global_total_order=false`，authority lane 响应中不存在 nonce/token/path/runtime secret/raw presentation。

## Workspace Contract

### Canonical workspace envelope

有效 Blueprint head 项目的 project read 必须从稳定 409 转为稳定 200 workspace projection。投影至少表达以下逻辑形状；实现可将 bounded history 拆为同源读 endpoint，但不得改变真源和能力语义：

```json
{
  "object": "creative.project_workspace",
  "schema_version": "1",
  "database_uuid": "exact database identity",
  "project_id": "proj_...",
  "title": "...",
  "status": "draft",
  "canonical_source": "creative_blueprint",
  "current_blueprint": {
    "database_uuid": "same identity",
    "revision": 4,
    "content_sha256": "...",
    "blueprint": "exact validated current Blueprint projection",
    "authority_projection": "exact validated projection as of revision 4"
  },
  "executable": {
    "state": "not_compiled",
    "current_timeline": null,
    "reason_code": "blueprint_timeline_compiler_unavailable"
  },
  "history": {
    "complete_project_history": false,
    "global_total_order": false,
    "replay_scope": "per_lane_only",
    "lanes": ["semantic_changes", "decision_authority", "legacy_artifacts"]
  },
  "capabilities": "server-derived capability projection"
}
```

同一响应中的 root `database_uuid`、current Blueprint `database_uuid`、Blueprint identity、authority `as_of_revision` 和 latest history windows 必须来自同一个一致读边界。读取 exact historical revision 时也必须返回同一个 database UUID；renderer 只有在 database/project/head identity 全部匹配时才能采用响应。

### Workspace resume

- `localStorage` 最多保存 project ID 和非权威 UI preference，不保存可被当作 current 的 Blueprint/authority/history 快照。
- 重启、路由切换或 backend runtime/database UUID 变化后，UI 必须从 Core 重建 workspace；在成功前不得短暂显示旧 brief 或缓存 Blueprint 为 current。
- project 不存在、database 已切换、读权限失效或完整性失败时，UI 显示稳定可恢复错误，清除这个 project 的“current”标记；不删数据、不选择其他项目冒充。

### Section diff and compare

Blueprint 的差异粒度冻结为 12 个 semantic sections：`intent`、`script`、`direction`、`output`、`constraints`、`material_hints`、`reference_refs`、`technique_refs`、`bindings`、`assumptions`、`missing_evidence`、`open_decisions`。

- history row 的 `changed_sections` 表示该 operation 的 parent→result 差异，不得冒充 current→selected 差异。
- 用户开启 compare 时，系统必须获取 exact current 和 exact selected revision，对 12 个 section 按 canonical value/digest 计算 current→selected 差异。P0 不承诺逐字 diff，但必须提供可读 section label、changed/unchanged 和两侧摘要。
- UI 本地计算的差异只作 presentation；Core restore 仍使用 exact digest 重新验证，不信任 renderer 声称的 changed sections。

### CAS restore

Restore 继续使用现有 desktop-authenticated `blueprint.restore_revision.v1`，不新增第二个写入路径。

1. UI 在用户确认前显示 target revision、current head、changed sections 和“将创建新 revision”，不说“删除后续历史”。
2. Desktop HTTP request 必须携带 query precondition `expected_database_uuid`，并携带 exact `expected_head={revision,content_sha256}` 与 exact `restore_from={revision,content_sha256}`；Core 在同一个写事务内验证 database UUID 与 head。actor/origin/authority/changed sections 不由 UI 填写。
3. command identity 由 exact database UUID、expected head 和 restore target 确定；响应丢失时只可用同一 idempotency key 重放，同一审阅事实不得随机产生多个 writer identity。
4. head conflict 后自动写入和自动重试次数为 0；UI 重新获取 current，要求用户在新差异上再确认。
5. success 后以服务端返回的新 head 为起点重读整个 workspace；不在 renderer 中手工拼接新 current state。

## Functional Requirements

- **FR-B2A-001**：有效 Blueprint head 项目的 project read 必须返回 canonical workspace，不再以 `creative_blueprint_workbench_unavailable` 拒绝；零 Blueprint/authority/capability trace 的 exact legacy-only project 保持现有 read/workbench 契约。受支持的 pairing/capability trace 在 head 缺失时仍必须 fail closed。
- **FR-B2A-002**：workspace current 只能由 explicit Blueprint head 解析；head、revision、operation、receipt、schema、digest 或 authority ledger 任一验证失败必须 fail closed，不回退 legacy brief/旧 Blueprint/缓存 UI state。
- **FR-B2A-003**：root/current/exact revision 的 `database_uuid`、project identity、head、current Blueprint、authority projection、executable capability 和同响应 latest history windows 必须来自一个一致读边界或 exact identity binding。
- **FR-B2A-004**：工作台必须在不依赖旧聊天的情况下呈现 current Blueprint 的 intent/script/direction/output/constraints/material hints/references/techniques/bindings/assumptions/missing evidence/open decisions，并保留实体 ID 供后续定位。
- **FR-B2A-005**：UI 必须将 canonical semantic state、creation authority、decision-authority projection 和 executable state 作为独立概念显示；不使用一个“已完成”标签合并。
- **FR-B2A-006**：authority 状态必须绑定 exact `as_of_revision`，显示 8 个 decision unit 状态；8/8 不推导 Timeline/render/export/publish authority。
- **FR-B2A-007**：executable projection 必须为 `not_compiled/current_timeline=null`，并以 server-derived capability 关闭 Blueprint Timeline create/edit/preview/render/export；不得只在按钮上关闭而保留可调用的 legacy path。
- **FR-B2A-008**：history 必须提供 `semantic_changes`、`decision_authority`、`legacy_artifacts` 三条 lane，每条都有 coverage/role/order/truncation 语义；顶层必须声明 `complete_project_history=false`、`global_total_order=false`、`replay_scope=per_lane_only`。
- **FR-B2A-009**：authority lane 只能返回 `event_id/sequence/authority_operation/observed_revision/decision_units/created_at`；nonce、token、receipt、runtime epoch/secret、path 和 raw presentation 全部不可出现。
- **FR-B2A-010**：legacy artifacts lane 只能返回 brief/Timeline identity、revision、created timestamp、有界 validation metadata 与诚实 role；lane/brief 为 `migration_context_only`，Timeline 为 `historical_observed_context_only`，order literal 固定为 `brief_revision_desc_and_timeline_created_at_desc_id_asc_revision_desc`。JSON 必须先做每值 1 MiB、聚合 16 MiB byte preflight，再做 canonical decode；不返回 locator/path/raw Timeline JSON，不把它们选为 head。
- **FR-B2A-011**：history/read 必须有界，semantic/authority 各最多 `history_limit<=100`，legacy brief+Timeline 共用同一 `history_limit`；只返回 latest window、available totals 与 `*_truncated`，不提供 cursor/has-more continuation，也不从 renderer 接受任意 SQL/order expression。窗口必须锚定 total 并保持上述域内连续顺序。
- **FR-B2A-012**：Blueprint history 必须显示 operation 的 parent→result `changed_sections`；current→selected compare 必须基于两个 exact validated revision 另行计算，不把两种 diff 混淆。
- **FR-B2A-013**：Restore 必须复用现有 Core command/desktop authentication/durable receipt，Desktop HTTP 强制 `expected_database_uuid` query precondition，并在同一写事务中使用 exact database identity、current head CAS 和 target digest；不增加 UI-only writer、raw DB write 或请求自报 authority。
- **FR-B2A-014**：Restore 必须通过新 operation/revision 表达，不删除、UPDATE 或回拨旧 revision/head；target 历史 authority 不得复制到新 revision。
- **FR-B2A-015**：Restore CAS conflict 后必须 0 次自动写重试；响应丢失时的 same-key replay 仍遵守 B0 永久 receipt 语义。
- **FR-B2A-016**：workspace resume 只信任 Core 重读；localStorage 不得保存或恢复 canonical Blueprint、authority 或可执行状态为真源。
- **FR-B2A-017**：若 semantic/authority canonical 验证失败，整个 canonical workspace 必须拒绝；若只有 legacy metadata lane 无法安全投影，该 lane 必须显式 `unavailable` 且不上升为 current，其余 canonical read 可保持可用。
- **FR-B2A-018**：B2A 不改 V1–V5 migration bytes/checksum 和 B1A exact V5 `sqlite_schema` allowlist；不新增或删除 table/index/trigger/view，不运行未受支持的 maintenance。
- **FR-B2A-019**：MCP 保持 `write=false`，paired CLI 仅保留 B1 已授权的 proposal commit/restore；B2A 不增加 Agent confirm/revoke、Timeline/render/export/file 能力。
- **FR-B2A-020**：B2A 不发起模型、网络、shell、subprocess、FFmpeg、任意路径、媒体读取/扫描、Creator Memory 写入或原文件移动/复制/删除/上传。

## Non-Functional Requirements

- **NFR-B2A-001 — Determinism**：相同 database UUID、project ID、exact head、available totals 与 bounded limits 必须生成 canonical-equivalent workspace projection；UI 显示顺序不改变语义。
- **NFR-B2A-002 — Boundedness**：任何 history lane 单次最多 100 条，网络响应不返回原始媒体、绝对路径、全量 raw ledger 或无界 script history。
- **NFR-B2A-003 — Consistency**：canonical workspace 必须是 snapshot-consistent；B2A 不分页。交易内审计证明必须绑定 exact connection/project/transaction epoch/total changes 与 main-schema 名称解析环境；受保护账本查询必须显式使用 `main.`，任何 DDL、ATTACH/DETACH、相关 PRAGMA 或受保护同名 TEMP object 都必须使旧证明失效或在签发前 fail closed。exact revision/restore/延迟响应必须验证 database/project/head identity，不得静默混合或让旧 child response 覆盖已切换 workspace。
- **NFR-B2A-004 — Accessibility**：所有状态不只靠颜色；键盘可完成 revision 选择、compare、restore 审阅和取消；焦点顺序稳定，错误使用 `aria-live`。
- **NFR-B2A-005 — Responsive UX**：支持 1440×1000 与 1280×800 桌面，390×844 无页面级横向溢出；移动触摸目标至少 44×44 CSS px。
- **NFR-B2A-006 — Honest language**：UI 文案必须使用“当前提案”、“已确认 n/8”、“尚未编译初剪”、“历史参考”等可验证表述，禁止“已定稿”、“可渲染”、“完整项目历史”等超出证据的文案。
- **NFR-B2A-007 — Local read budget**：在冻结的 100 Blueprint operations + 100 authority events + 100 legacy metadata 合成 fixture 上，workspace/read-history focused benchmark 的本机 p95 目标为 500 ms 内。交易内 typed ledger attestation 仅复用当前 SQLite 快照已完整审计的 Blueprint head/解析结果，authority event/receipt 全链仍完整校验，不引入跨事务 cache 或 bypass。25 次 warm read 的 p50/p95/max 为 `220.310 / 227.083 / 230.360 ms`，p95 较最初 `1760.719 ms` 降低 87.1%，通过 500 ms 硬门槛；测试对该门槛直接断言。

## Visual Acceptance

### Desktop — 1440×1000 和 1280×800

- 首屏可同时辨认 project title、current Blueprint revision、proposal 身份、confirmed count 和 `not_compiled` 状态。
- 主区按 12 个 section 分组显示 current Blueprint；长 script/material hints 可展开，但不将整个原始 JSON 作为默认 UX。
- 历史面板保留 3 条 lane 标签，semantic row 可打开 section diff；authority/legacy row 不出现伪造的 restore/execute 入口。
- Compare 显示 current 与 target 的 revision identity、changed sections 和 restore 后果；Restore 是二次审阅动作，取消永远无写入。
- Storyboard/Timeline/Preview 区不显示 legacy Timeline 为 current；用诚实 empty state 说明 compiler 是下一切片。

### Mobile — 390×844

- 无页面级横向溢出，不强行缩小桌面密集 Timeline。
- Blueprint sections、authority units 和 history lanes 降级为纵向 card/accordion，标题和状态在不展开时仍可见。
- Compare/restore 使用全宽 sheet/page，固定展示 current/target 身份与取消；不在水平表格中截断关键差异。
- 所有交互目标至少 44×44 CSS px，焦点可见，状态不仅靠色彩。

截图验收至少覆盖：healthy current、partial authority、compare+restore review、CAS conflict、canonical integrity failure、legacy lane unavailable、compiler unavailable，以及 legacy-only project 回归。每个关键状态在 1440×1000 和 390×844 至少有一张可审查工件；可按状态组合减少总数，不得用静态设计图代替真实 renderer/browser 截图。

## Failure-Closed Matrix

| Failure | Required result | Forbidden fallback |
| --- | --- | --- |
| explicit head missing but Blueprint history exists | canonical integrity error | `MAX(revision)`、legacy brief |
| head/revision/content digest mismatch | canonical integrity error | cached/older Blueprint |
| Blueprint operation/receipt/schema corruption | canonical integrity error before workspace current | partial current document |
| authority ledger/projection corruption | canonical workspace unavailable | show all units unverified as if verified read succeeded |
| latest history window totals/order no longer bind current snapshot | integrity/stale projection，refetch | merge two snapshots silently |
| legacy artifact metadata invalid | `legacy_artifacts` lane unavailable | select artifact as current or decode raw path/JSON |
| restore current head changed | CAS conflict, 0 automatic mutation retry | last-write-wins or silent rebase |
| restore target digest changed/corrupt | target/integrity error, 0 mutation | choose same revision number with different digest |
| backend/database UUID changed after renderer resume | rebuild workspace from Core | retain old project state as current |
| compiler absent | `not_compiled`, Timeline actions disabled at Core and UI | call legacy Timeline create/revise/render |

## Capability Boundary / Non-goals

B2A 明确不实现：

- Blueprint→Coverage/Timeline compiler、compiler manifest、Timeline binding 或 V6 schema。
- Global Assignment、逐段素材匹配、气口/声画编译、Craft Card 执行、字幕或调色。
- Blueprint 项目的 Timeline edit、preview、render、export、usage ledger 或素材包。
- 覆盖所有域的 unified project operation table/total order/replay，以及跨域 undo/redo/branch。B2A 只提供已有 Blueprint restore 的可视化入口。
- renderer 内的 semantic confirm/revoke；该高权威写入继续归 Electron main 原生审阅。
- 新 project 创建、legacy backfill/auto-adopt、brief 删除/重命名或数据库 repair。
- 新 Agent/MCP 写入能力、新模型 key、网络调研、媒体读取或任何原文件操作。

## Success Criteria

- **SC-B2A-001**：healthy Blueprint-head project 从 project read 恢复成功率 100%，legacy brief 被呈现为 current 次数为 0。
- **SC-B2A-002**：0/8、partial、8/8 与 post-edit invalidation fixture 中，semantic/authority/executable 投影与 Core exact revision 一致率 100%，8/8 自动产生 executable 次数为 0。
- **SC-B2A-003**：对至少 50 步 commit/no-change/restore/confirm/revoke 混合历史，3 条 lane 的域内次序/身份一致率 100%，声称 complete/global order 次数为 0。
- **SC-B2A-004**：authority lane 的 secret/path/raw presentation 泄露矩阵命中 0 次，legacy lane 的 raw Timeline/locator 泄露 0 次。
- **SC-B2A-005**：12-section history diff 和 current→selected compare 在冻结 fixture 上与 Core canonical section digests 一致率 100%。
- **SC-B2A-006**：至少 100 轮 concurrent Agent commit vs UI restore 中静默覆盖为 0，CAS conflict 后自动写重试为 0，same-key lost-response replay 额外 domain mutation 为 0。
- **SC-B2A-007**：所有 Blueprint integrity/authority corruption fixture 的 legacy/cached fallback 为 0；仅 legacy lane 损坏时 canonical semantic 与 authority 投影仍保持诚实。
- **SC-B2A-008**：V1–V5 migration bytes/checksum、normalized `sqlite_schema` allowlist 与 schema version 在实现前后 exact-equal；新数据库对象数为 0。
- **SC-B2A-009**：Blueprint project 的 legacy Timeline create/revise/new render 成功数为 0；legacy-only project 的现有 Create/Timeline 回归全部通过。
- **SC-B2A-010**：1440×1000 和 390×844 真实 UI 验收无 console error、无 broken image、无页面级横向溢出，关键诚实状态都有截图证据。
- **SC-B2A-011**：focused backend/Core/renderer 套件、B0/B1/B1A authority/integrity 回归、plugin safe-reader 回归和一次完整 `npm run check` 均通过；未跑 remote CI 时状态仍为 `NOT RUN`。

## Implementation and Admission Status

当前 checkout 已实现 canonical workspace read、3 条诚实 latest-window history lane、exact revision compare、Desktop-authenticated CAS restore bridge、database/project identity adoption guard 与响应式 Workbench。它没有新增 migration/schema、compiler、Timeline binding、unified ledger 或 MCP writer。

当前本地证据支持“功能已实现，并经过 focused/浏览器只读/并发/性能与 final-diff full gate 验证”；不支持“所有验收已通过”：T002/T004/T005 要求的实施前 v4→v5 snapshot/baseline 与 red-before-green transcript 未保留；1280×800、真实 Electron Desktop 可写 restore/CAS conflict 视觉流程、完整 keyboard/focus 验收与 repo-local 截图归档未完成；Remote CI 未运行。详情见 [implementation-evidence.md](implementation-evidence.md)。

## Release Gate

B2A 只有在以下条件都有当前 diff 证据后，才能从当前 `IMPLEMENTED / LOCAL VALIDATION WITH RESIDUALS` 晋级为 `COMPLETE / LOCALLY VALIDATED`：

1. 先有 failing tests 证明现有 409/legacy fallback 断点，再有实现后绿色结果。
2. canonical/authority/history/restore 的对抗测试与隐私负例通过。
3. V5 exact-schema/checksum oracle 证明零 schema 变化。
4. Blueprint compiler-unavailable 和 legacy Timeline write/render 阻断仍通过。
5. 桌面/移动真实截图、键盘/焦点检查与 console/overflow 证据齐全。
6. 当前 diff 上完整 `npm run check` 通过，独立复审无未解决 P0/P1。
7. [implementation-evidence.md](implementation-evidence.md) 只记录实际运行的命令、计数、截图和边界，不用本 Spec 的预期替代证据。
8. 100+100+100 focused benchmark 达到 500 ms p95 门槛；当前冻结 fixture 的 p95 为 `227.083 ms`，该条已满足。

GitHub-hosted CI、tag、release 或 hosted validation 不会因本地通过而自动成立。

## Follow-up Boundary

- `ML-015-B2B`：在独立 Spec 中定义 Blueprint→Timeline deterministic compiler、evidence/compiler manifest、immutable binding、stale detection 和必要的 V6 migration；本切片不预先修改 B1A closed-world allowlist。
- `ML-015-B2C`：只有当 Blueprint、Coverage、Timeline、Preview/UI mutations 都有同一预条件和 replay 语义时，才定义 unified project operation history、undo/redo/branch；B2A 的 3 lanes 是过渡性诚实投影，不是其替代品。
- `ML-018`：负责 script coverage/global footage assignment，不得把 B2A `material_hints` 的展示误当成已完成全片分配。
