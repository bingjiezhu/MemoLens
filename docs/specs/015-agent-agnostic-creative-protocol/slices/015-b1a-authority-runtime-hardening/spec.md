# Feature Specification: Authority Runtime Hardening

- Feature ID：`ML-015-B1A`
- 创建日期：2026-08-22
- Spec 状态：`FROZEN`
- 实现状态：`IMPLEMENTED`
- 验证状态：`LOCALLY VALIDATED`
- Remote CI：`NOT RUN`
- 实施授权：用户已授权按 Grill Me 共识继续逐片实现
- 父规范：[ML-015 Agent-Agnostic Creative Protocol](../../spec.md)
- 前置切片：[ML-015-B1 Paired Agent Proposal & Decision Authority](../015-b1-paired-agent-decision-authority/spec.md)
- 实施证据：[implementation-evidence.md](implementation-evidence.md)
- 发布目标：`0.10.1` maintenance；不改变 V5 schema 与能力边界

## Outcome

B1A 修复 B1 已记录的三个确定性运行时缺口，关闭实现过程中发现的 SQLite writer 阻断项，并把两轮独立 Core 审计发现的共八个 P1 完整性/有界性缺口固化为合同。本切片已在本地实现和验证，但没有 remote CI 或 hosted validation 证据，也不增加任何创作能力或权限：

1. 同一 `MediaRepository` 运行期内，健康 Blueprint commit/restore 不再对已验证历史重复做 `O(R)` 全链 decode；冷启动、外部数据库提交或观察异常仍完整审计。
2. Electron main 的 authority/pairing/revoke 请求在 12 秒内没有完成 fetch 与有界 body read 时稳定超时，presentation 超时不得进入原生审阅或后续 mutation。
3. CLI credential 通过同一个文件描述符执行 `O_NOFOLLOW + O_NONBLOCK + fstat + bounded read`，不再先检查路径再由 `Path.read_bytes()` 重新打开，也不允许最终组件竞态替换为 FIFO 后无限阻塞。
4. Desktop/backend 写入运行时在任何目录创建、数据库连接或 managed process spawn 前证明实际链接的 SQLite 已包含 WAL-reset 修复；受影响、未知或不可证明的运行时 fail closed。只读 Agent plugin 不因此抬高 Python 3.10+ 的兼容下限。
5. Blueprint command 的事务连续性、same-connection canonical database filename 绑定、cold schema allowlist、fallback exact delta 与整段历史 prefix 证明成为显式合同；任一项不可证明时不得产生“成功但缺 receipt”、持续写入已观测为替换对象的路径，或接受重写历史。
6. Electron 只在确认旧 backend 已 `exit/close` 后启动替代进程或完成应用退出；Flask debug 永不启用 reloader，防止第二 writer 逃离 main-process ownership。
7. backend health body 无论 `Content-Length` 或 streaming 都不得超过 8 KiB；状态改变 HTTP JSON 在进入递归 decoder 前先做 32 层 lexical depth 预检。
8. Core 第二轮复核要求 cold schema 比较 normalized SQL、warm prefix SQL 使用有界 rowid range plan、successor 同时验证 immutable vector 与 mutable typed state，并使每表 frontier 跟随 global rowid high-water；从而拒绝 equal-count DML substitution，也避免反复扫描其他项目的历史。

本切片把“快”建立在可证明的新鲜度上，而不是关闭历史验证。

## User Stories

### US1 — 长项目继续流畅创作（P0）

作为持续迭代同一视频方案的创作者，我希望第 100 次与第 1000 次 proposal/restore 不因为健康旧历史而越来越慢，同时旧记录被外部修改后系统仍必须拒绝继续写。

**独立验收**：同一 repository 的 warm append 只验证本次 delta；新 repository 或第二 SQLite connection 修改旧 operation/revision/receipt/capability 后，下一次写先完整审计并 fail closed。

### US2 — 本地后端卡死可恢复（P0）

作为正在审批 Agent 配对或创作决定的用户，我希望本地 backend 不返回 header/body 时界面能在固定期限结束等待，而且不会在没有看见 exact presentation 时产生任何写入。

**独立验收**：pairing、capability revoke、decision authority 的 never-resolving fetch/body 均返回 `authority_request_timeout`；native review 与后续 mutation 调用数均为 0。

### US3 — 配对凭据读取边界真实生效（P1）

作为本机创作者，我希望 CLI 只从已经安全打开并复验的普通私有文件读取最多 16 KiB，而不是在安全检查与读取之间重新解析可被替换的路径。

**独立验收**：loader 不调用 `Path.read_bytes()`；打开 flags 包含平台可用的 `O_NOFOLLOW`，权限与普通文件判断来自同一 fd 的 `fstat`，累计读取最多 16,385 bytes。

### US4 — 已知不安全的 SQLite 不得承载 Desktop 写入（P0）

作为把素材与创作历史交给 MemoLens 的创作者，我希望 Desktop 不会因为“Python 版本看起来够新”就在 SQLite 官方已确认可能损坏 WAL 的运行时上启动多连接写入。

**独立验收**：版本判据来自单一标准库模块；受影响 runtime 在 0 次用户数据库连接、0 次目录/迁移写入、0 次 backend spawn 前返回稳定错误；修复 runtime 才可启动，`/healthz` 的 HMAC 身份证明还必须同时携带不泄露路径的 runtime capability。

## Architecture Decision: Verified-Prefix Lease, Not a Self-Attesting Checkpoint

本切片不新增持久 checkpoint 表。存放在同一 SQLite 文件里的 checkpoint 可以作为派生审计证据，却不能独立证明同库历史没有被一起改写；让它直接跳过冷启动审计会制造虚假信任根。

采用 bounded process-local `VerifiedPrefixLease`：

```text
首次写 / restart / observer change
  -> BEGIN IMMEDIATE
  -> existing full Blueprint + receipt + capability audit
  -> bind same-connection canonical main filename + pathname identity/schema/project/head/frontiers
  -> compute domain-separated rolling commitments over every immutable project-table prefix
  -> publish process-local lease only after successful commit

同一 runtime 后续写
  -> same long-lived SQLite change observer
  -> PRAGMA data_version unchanged
  -> exact database/schema/project/head/frontiers and historical-prefix proof match
  -> private transaction context exposes the attested prefix
  -> validate only newly appended operation/revision/receipt/use event
  -> atomically advance head and renew lease

任何不一致 / observer unavailable / external commit
  -> discard lease
  -> full audit; never silently continue on the fast path
```

约束：

- lease 只存在于一个 `MediaRepository` 实例与创建它的 PID 内，必须有固定上限与 LRU 驱逐；fork/PID 变化必须关闭继承的 anchor 并冷启动，不得落盘、序列化或通过 API 暴露。
- change observer 必须是同一条长寿命 SQLite connection；只比较该 connection 前后 `PRAGMA data_version` 是否相等，不把数值当全局序号。
- warm Blueprint command 使用 observer-owned、加锁的 transaction；其他 repository transaction、其他实例与测试/诊断 SQL 均是 external change，会使 observer 的 data version 改变。
- fast-path context 只能由 repository 在完整审计或有效 lease 后内部创建；不得增加 caller-controlled `skip_validation`、环境变量或 HTTP/CLI/MCP 开关。
- context 必须绑定当前 connection 通过 `PRAGMA database_list` 报告的 canonical main filename、该 pathname 的多点 identity、database UUID、schema version、project、旧 head、operation/revision/receipt/capability closure frontiers 与所有 immutable project-table prefix commitments，并在 rollback、异常、observer 变化时销毁。只做 `_connect()` 前的单次 pathname `stat` 不充分；本切片也不宣称获得标准库 `sqlite3` 未暴露的 VFS fd/inode 密码学证明。
- restore 可按需读取单个已证明前缀内的 target，但不得重新扫描从 revision 1 到 target；本次新 revision、restore binding、result、receipt 与 capability-use 必须完整验证。
- public read、迁移、schema verification 和 cold audit 继续使用现有全量验证器；旧验证器不得删除或弱化。
- 进程崩溃后没有 lease，下一进程必须重新 `O(R)` 完整审计。B1A 不承诺 crash-persistent fast path。
- long-lived WAL anchor 只可在不受已知 WAL-reset corruption bug 影响的 SQLite runtime 启用。SQLite 官方记录该 bug 影响 3.7.0–3.51.2，并在 3.51.3 修复，同时提供 3.44.6 与 3.50.7 backport；已撤回的 3.52.0 也不进入 allowlist。允许范围冻结为 `3.44.x >= 3.44.6`、`3.50.x >= 3.50.7`、`3.51.x >= 3.51.3` 或 `>= 3.53.0`。Core 的私有 optimization gate 在 unsafe/unknown runtime 不创建 anchor；更外层的 Desktop/backend writer admission 必须在接触用户数据库前 fail closed，不能把 full-audit fallback 误当成 WAL corruption 修复。参见 [SQLite WAL documentation](https://www.sqlite.org/wal.html#the_wal_reset_bug) 与 [PRAGMA data_version](https://www.sqlite.org/pragma.html#pragma_data_version)。

### Prefix proof contract

process-local floor 不能只固定 frontier operation/head/receipt。每个 cold audit 必须对所有 10 张 immutable project table 的 rowid-bounded prefix 计算 domain-separated rolling commitment，再将每表 `(table,max_rowid,row_count,sha256)` 合成 versioned lease attestation。`max_rowid` 是该表的 global high-water，即使当前 project 在该区间没有行也必须前进；warm successor 只能查询 `(prior_global,current_global]` 范围内属于当前 project 的有界新行，且 query plan 必须使用 SQLite integer-primary-key rowid range，不得退化为全表/项目历史 scan 或 temp B-tree。冻结表覆盖 operation、revision、desktop/agent receipt integrity、capability fact/event、pairing receipt 与 decision-authority event/receipt；capability ledger 自身仍由其 hash chain/frontier 独立验证。

同一 repository 已信任 prefix 之后，另一 writer 可以合法 append；新 cold audit 必须证明旧 prefix digest 完整保留后才接受 extension。旧 sequence 中任意非前沿字段被改写，即使当前 frontier 未变且又有合法新 append，也必须 fail closed。本合同仅提供 repository/process lifetime 内的 monotonic proof；跨进程 anti-rollback 仍是 non-goal。

## Controlled Safety Amendment: Writer Runtime Admission

冻结 Spec 后的本机证据显示：当前 `.venv` 是 Python 3.11.11 + SQLite 3.47.1，而 MemoLens 已有 Flask 请求线程、media/render worker、多个 `MediaRepository._connect()` 连接与自动 checkpoint。该组合满足 SQLite 官方 WAL-reset bug 的触发前提。原媒体文件不会因此移动或删除，但 canonical Blueprint/media ledger 可能损坏，因此这是 B2 前 P1 release blocker，而非可留作文档提醒的性能问题。

准入规则：

- `core/sqlite_runtime.py` 是版本判据、capability payload 与 stable error 的唯一来源；bootstrap、backend、health 和 Electron preflight 不得复制阈值。
- Python 3.14 只是当前推荐的 managed runtime，不是安全证明；必须检查 `sqlite3.sqlite_version_info`。
- Desktop `create_app()` 在 Flask app、Settings、目录、DB、migration 或 runner 之前调用准入；失败码为 `sqlite_wal_reset_unsafe`。
- Electron 对已经在线的 backend 必须同时验证 session HMAC 与 `sqlite_runtime.wal_reset_safe=true`；auto-start 前对保存的 `pythonCommand` 运行无 shell、短 timeout、bounded-output probe，失败时 0 spawn。
- health capability 只含 library version、`wal_reset_safe` 与 journal policy，不得包含 Python executable、数据库路径或 app-state 路径。
- setup 必须重建“Python 主版本合格但 SQLite unsafe”的旧 `.venv`；显式 `MEMOLENS_PYTHON` unsafe 必须清晰失败，不能悄悄换解释器。
- 不切换 `journal_mode=DELETE`，不顺带修改 `synchronous=NORMAL`；这两项分别涉及 plugin snapshot contract 与独立 durability trade-off。

## Controlled Safety Amendment: Audit Closure Contracts

独立 Core 审计在基础 fast-path 测试全绿后仍构造出四类可导致误信任的路径。它们是 release-blocking contract，不能降级为 residual risk：

1. **Transaction continuity**：yielded callback/extension 不得通过 `commit`、`rollback`、`executescript`、transaction SQL 或清除 authorizer 切断受管事务。系统必须使用不可由 callback 伪造的连续性证明，而不是只检查退出时 `in_transaction=true`。一旦边界可能已被跨越，必须永久 poison 当前 repository 的 Blueprint authority，不能用新事务的 rollback 宣称已恢复原子性。
2. **Atomic success closure**：成功 response 必须与 operation、revision/no-change、head、desktop/agent receipt 和 capability-use 组成一个同事务、精确 DML 数量与链接的闭包。fallback 不得传入 `context=None` 跳过 successor validator；cold 后的额外表/项目写入、额外 receipt trigger 或“改了再改回去”都必须被拒绝。
3. **Connection/path binding**：认证不得只使用 `_connect()` 前的 pathname `stat`。当前 connection 必须通过 `PRAGMA database_list` 只报告一个 `main`，其 canonical filename 必须等于 repository path，并与 repository-lifetime `(st_dev, st_ino)` 在 begin/audit/pre-commit/post-commit 多点复验绑定；任一持续 mismatch 都 poison repository。本合同明确不引入 `ctypes` 解析 CPython 私有 `_sqlite3` ABI，也不把该组合证据夸大为 VFS-level opened-fd inode proof。当前 OS 用户在所有复验点之间完成 A→B→A rename 的极窄窗口作为明示 residual P2，不属于跨本机用户隔离承诺。
4. **Exact cold boundary and historical prefix**：cold audit 必须对 V5 允许的 `sqlite_schema` 对象集做 `(type,name,tbl_name,normalized_sql)` exact allowlist（包括允许的 SQLite 内部 auto-index），复用已有 physical-definition validators，并以 command 前后 normalized exact snapshot 拒绝执行中定义改变；任何额外 table/index/trigger/view 或同 identity/不同 SQL 都必须被拒绝。同时按上述 prefix proof contract 证明整段已信任历史，不能只 pin frontier。

### Backend lifecycle and transport closure

- managed backend 终止是 single-flight barrier：先 `SIGTERM` 并有界等待 `exit/close`，再按需 `SIGKILL` 并再次有界等待。如果仍无法证实终止，必须保留旧 process ownership、拒绝 replacement spawn，并阻止把 app quit 误标为已完成。旧 process 的延迟 event 不得清除新 process ownership 或旋转新 runtime credential。
- Flask `debug=true` 只启用调试诊断，`use_reloader` 恒为 `false`。
- health reader 必须同时约束声明长度与实际 stream，最多 8,192 bytes；超限必须 cancel/release 且 trust=false。
- 状态改变 HTTP JSON 在 `json.loads` 前必须对原始 UTF-8 text 执行 string-aware lexical container-depth 预检，上限 32；预检只防止 decoder recursion，后续仍必须执行 duplicate key、node/member/item/string/number 与 canonical-size 限制。

## Controlled Safety Amendment: Second Core Audit Closure

第二轮独立 Core 复核在初轮四个 P1 红转绿后又发现四个 P1。这些缺口不是原有四项的重复表述，而是对“exact”与“bounded”的更强证明：

1. **Schema identity without normalized SQL**：仅比较 `(type,name,tbl_name)` 会接受同名但列、排序或 predicate 已改变的 index/trigger/view。cold allowlist 现必须比较 `(type,name,tbl_name,normalized_sql)`；同 identity 但 SQL 不同的 replacement 在 mutation callback 前拒绝。
2. **Constant statement count without a bounded plan**：SQL 语句数不随 revision 增长，并不证明每条查询没有扫描全表。warm immutable-prefix query 现必须显式约束 `rowid > old_global AND rowid <= new_global`，并以 `EXPLAIN QUERY PLAN` 证明使用 `INTEGER PRIMARY KEY` 的双边 rowid range，无 `SCAN` 或 temp B-tree。
3. **Equal-count successor substitution**：`total_changes` 等于预期值不能证明改的是预期行。successor 现必须同时验证 10 表 immutable delta vector、完整 typed `creative_projects` row、exact head/operation timestamp relationship，并要求 `total_changes = immutable_changes + mutable_changes`。用一次 unrelated DML 替换 project touch，或在相同 change count 下改坏 title/status/timestamp，均必须回滚。
4. **Project-local frontier without global high-water**：若每表 frontier 只记当前 project 的最大 rowid，其他 project 新增的大量行会在当前 project 每次 warm command 中被重复扫描。每表 frontier 现必须保存 global `MAX(rowid)`，即使当前 project 命中 0 行也向前推进；32 条跨项目 paired history 后的下一次 warm command 只查询空的新 global interval。

### Closed-world SQLite maintenance rule

Blueprint authority 把 managed V5 schema 视为 closed world。外部 `ANALYZE` 可创建 `sqlite_stat1`/`sqlite_stat4` 等统计对象；这些对象不在 allowlist 中，因此下一次 Blueprint authority command 必须在 mutation 前以 `blueprint_schema_integrity_error` fail closed。这是刻意的保守运维边界，不是自动修复信号。

运维者不得对活跃 MemoLens 数据库手工执行 `ANALYZE`、创建/编辑 `sqlite_stat*`、`VACUUM` 或重建 schema object。如已发生，必须停止 Blueprint 写入、保留原库与 WAL 证据，并从可验证备份恢复或使用未来明确支持的 maintenance/repair 工具；不得为了“让检查变绿”而直接删改 SQLite 内部表。

## Functional Requirements

- **FR-B1A-001**：每个 repository 的 verified-prefix lease 数量必须有界，默认最多 16 个项目；驱逐只影响性能，不影响正确性。
- **FR-B1A-002**：lease 首次建立前必须完成现有 Blueprint revision、operation、desktop/agent receipt、sidecar 与相关 capability ledger 全审计。
- **FR-B1A-003**：fast path 前必须在写锁内验证 observer data version、database UUID、schema version、project 与 exact persisted frontiers；任一不一致必须丢弃 lease。
- **FR-B1A-004**：只有当前受管 transaction 的 delta 全部验证且 commit 成功，才可发布新 lease；rollback、commit failure、异常或并发外部 change 不得发布。
- **FR-B1A-005**：warm commit、no-change、restore、desktop receipt、paired receipt 与 capability-used event 都必须有 delta validator；不得只验证 head。
- **FR-B1A-006**：idempotency replay 必须继续验证 exact receipt、linked operation/effect 与 request binding；replay 不得产生新 mutation 或错误推进 lease。
- **FR-B1A-007**：第二 SQLite connection 的合法写或篡改、另一 repository 实例、database identity/schema/path 变化、observer failure 均必须使 fast path失效；恢复路径是 full audit 或 fail closed。
- **FR-B1A-008**：不得修改 V1–V5 schema/checksum，不新增持久 checkpoint，不减少 immutable trigger 或 schema preflight。
- **FR-B1A-009**：Electron 所有 main-authority 与 desktop-authority HTTP exchange 必须共享固定 `12_000 ms` deadline，并覆盖 fetch 与完整 bounded response-body read。
- **FR-B1A-010**：authority timeout 必须 abort request、取消/释放 body reader、清理 timer，并稳定映射为不含 path/credential 的 `authority_request_timeout`。
- **FR-B1A-011**：presentation/read timeout 后 native review 与后续 mutation 必须为 0；mutation response timeout 仍由既有 idempotency receipt/reconciliation 语义处理，不得谎称服务端一定未提交。
- **FR-B1A-012**：credential load 必须使用一个 fd 完成 open、fstat、permission/type check 与读取；最终组件不得跟随 symlink。
- **FR-B1A-013**：credential reader 最多取得 16,385 bytes；第 16,385 byte 只用于稳定识别超限，超限返回 `agent_credential_invalid`。
- **FR-B1A-014**：credential secret、main authority token、desktop token、runtime epoch 不得进入新增错误、测试日志或普通输出。
- **FR-B1A-015**：MCP 保持 `write=false`；paired CLI 仍只可 commit/restore 已有项目的 unverified proposal；Agent 仍不能 confirm/revoke。
- **FR-B1A-016**：verified-prefix anchor 必须执行 SQLite capability/version gate；当前 runtime 无 `data_version` 合同或命中 WAL-reset affected range 时不得创建 anchor，功能正确性必须退回现有 full audit。
- **FR-B1A-017**：PID 变化不得复用继承的 SQLite connection、lock 或 lease；child 第一次命令必须创建自己的安全 runtime 并完整审计。
- **FR-B1A-018**：SQLite runtime 判据必须由一个纯标准库 Core 模块提供；版本矩阵必须拒绝 3.44.5、3.47.1、3.50.6、3.51.0、3.51.2、3.52.0，接受 3.44.6、3.50.7、3.51.3 与 3.53.0+。
- **FR-B1A-019**：backend writer admission 必须在任何用户 DB connect、目录创建、migration 或 runner 构造前 fail closed；错误码稳定为 `sqlite_wal_reset_unsafe`。
- **FR-B1A-020**：Electron 必须拒绝缺失/false/malformed SQLite health capability；auto-start probe 必须无 shell、带 timeout 与 output 上限，失败时 backend spawn 数为 0、trust 为 false。
- **FR-B1A-021**：只读 plugin 的 Python 3.10+ 合同保持不变；Core/Desktop CI 必须在 safe SQLite runtime 上运行，并把 plugin compatibility 拆成独立 lane。
- **FR-B1A-022**：受管 Blueprint callback 不得逃逸原事务；必须用 authorizer 阻止与事务连续性证明同时检测 direct/base-descriptor/cursor/`executescript` 路径。可能已发生 partial commit 时必须 poison repository 并拒绝后续 Blueprint authority。
- **FR-B1A-023**：所有成功 commit/restore（warm 和 fallback）都必须具有 operation/revision-or-no-change/head/receipt/capability-use 的 exact successor context；没有 context 或任何额外 DML 必须 fail closed。
- **FR-B1A-024**：cold audit 必须将 `main.sqlite_schema` 的实际 object set 与冻结 V5 allowlist 做精确匹配，并在 command 前后保持 exact snapshot；额外 table/index/trigger/view、attached database 或 temp object 数量均为 0。
- **FR-B1A-025**：当前 SQLite connection 必须用 `PRAGMA database_list` 报告的 canonical main filename 与 repository path 一致，并与 repository-lifetime pathname identity、database UUID 与 schema 在 begin/audit/pre-commit/post-commit 绑定。可观测的 same-UUID clone replacement 必须被拒绝；不得仅依赖 connect 前单点 `stat`，也不得宣称已获得 VFS opened-fd inode proof。
- **FR-B1A-026**：process-local monotonic floor 必须包含整段已验证 project prefix 的 versioned digest。另一 repository 合法 append 后，原 repository 的 cold re-audit 必须在接受新 frontier 前证明旧 prefix digest。
- **FR-B1A-027**：Electron stop/restart/quit 必须等待旧 child 确认终止；`SIGTERM` 失败后必须尝试 `SIGKILL`，两者均无法确认时保留旧 ownership 并拒绝启动替代 child。
- **FR-B1A-028**：backend 即使启用 debug 也必须 `use_reloader=false`，Electron 管理的一次 startup 只能产生一个 backend writer root process。
- **FR-B1A-029**：Electron health response 声明长度和实际读取长度均不得超过 8,192 bytes；非法/过大长度、stream 超限、无法 parse 均不得建立 backend trust。
- **FR-B1A-030**：状态改变 HTTP JSON 必须在 decoder 前执行字符串感知的 lexical nesting 预检，深度上限 32；括号字符串不得误报，深层输入不得先触发 Python recursion failure。
- **FR-B1A-031**：cold managed-schema allowlist 必须精确比较 `(type,name,tbl_name,normalized_sql)`；对象名称相同但 SQL definition 不同的 table/index/trigger/view 必须在 mutation 前拒绝。
- **FR-B1A-032**：warm immutable-prefix query 必须只读取 global rowid 区间 `(old_high_water,new_high_water]`；其 `EXPLAIN QUERY PLAN` 必须是 integer-primary-key rowid 双边 range，不得出现 full scan 或 temp B-tree。
- **FR-B1A-033**：successor validator 必须校验按 principal/result-kind 确定的 10-table immutable delta vector、完整 typed `creative_projects` state、exact head/operation timestamp binding 与 `total_changes` 守恒；相同 change count 的 unrelated/mutable substitution 必须拒绝。
- **FR-B1A-034**：每个 immutable table commitment 的 `max_rowid` 必须是该表 global high-water，不是当前 project 的局部最大值；其他 project 历史不得在当前 project 的每次 warm command 中被重复扫描。
- **FR-B1A-035**：外部 `ANALYZE`/`sqlite_stat*`、`VACUUM` 或未授权 schema rebuild 造成 closed-world boundary 变化时必须 fail closed；MemoLens 不得自动删改 SQLite 内部对象或伪装自动恢复。

## Stable Errors

| Code | Surface | Meaning |
| --- | --- | --- |
| `authority_request_timeout` | Electron | 12 秒内未完成 authority fetch/body read；未从 presentation 继续动作 |
| `agent_credential_insecure` | CLI | credential 不是同一 fd 上复验的普通私有文件，或最终组件为 symlink/发生身份替换 |
| `agent_credential_invalid` | CLI | credential 超过 16 KiB 或内容/合同无效 |
| `sqlite_wal_reset_unsafe` | Setup/Core/Desktop | 实际链接的 SQLite 未证明包含 WAL-reset 修复；未接触用户数据库或启动 backend |
| `blueprint_schema_mutation_forbidden` | Core/API | callback 尝试事务/连接/schema control；原事务尚可证明时回滚当前 command |
| `blueprint_post_commit_verification_indeterminate` | Core/API | callback 事务连续性或 commit 后边界无法证明；当前 repository 不得继续 Blueprint authority |
| `blueprint_database_binding_indeterminate` | Core/API | connection-reported canonical main filename 或 pathname/UUID/schema 多点绑定无法继续证明 |
| `blueprint_schema_integrity_error` | Core/API | cold exact allowlist 或 command 前后 schema boundary 失配 |
| `blueprint_command_delta_invalid` | Core/API | mutation 不是约定的 exact successor，或含额外 DML 副作用 |
| 既有 `blueprint_*_integrity_error` | Core/API | lease 失效后的 full audit 或 delta validation 发现损坏 |

verified-prefix miss 不是用户错误，不新增“cache miss”错误；它必须透明回退完整审计。

## Non-goals

- V6 schema、持久 rolling hash/Merkle checkpoint、签名审计根或跨进程 warm cache。
- capability 全历史 checkpoint、authority ledger checkpoint 或原生 1 MiB 虚拟化 review UI。
- Blueprint→Timeline compiler、render、export、素材包、发布状态或任意文件移动。
- write MCP、远程 pairing、跨 OS 用户的 credential 保密、keychain/硬件身份。
- 把 Python 3.14 设成 plugin 最低版本、自动在线安装 Python、静默改为 DELETE journal 或顺带改变 synchronous durability policy。
- 依赖 timer 推断 mutation 是否提交；永久 receipt 仍是 definitive replay/reconciliation 依据。

## Edge Cases

- 第一次 command 是 no-change replay；不得在没有完成 cold audit 时建立“空”lease。
- restore target 很旧、target 本身来自 restore；只读取有界相关行，仍验证 target identity/content 与本次 restore binding。
- transaction 内已插入 operation 但 receipt 写入失败；回滚后 lease 必须仍指向旧 prefix或被丢弃。
- observer 在 audit 前后变化；不得发布混合 snapshot，必须重试有界次数或 fail closed。
- backend 正在读 response body 时到达 deadline；reader cancel/release 与 timeout 映射只能发生一次。
- `O_NOFOLLOW` 平台不可用；lstat 与同 fd fstat identity comparison 仍必须阻止检查/打开身份不一致，能力不得被夸大为跨同用户密码学隔离。
- lstat 后 regular file 被换成 FIFO/socket/device；平台可用时 `O_NONBLOCK` 必须保证 open 不会在 fstat/type reject 前无限等待。
- settings 保存了旧的 unsafe Python command；每次 launch 必须重新 probe，不能只修默认解释器选择。
- backend health HMAC 正确但 capability 缺失或 false；Electron 仍必须拒绝信任。
- callback 清除 authorizer，然后 `commit; BEGIN IMMEDIATE` 并抛异常；即使退出时 `in_transaction=true`，也必须检出连续性已丢失并 poison repository。
- pathname 在 connect/audit/commit 周边替换成 same-UUID clone 并在任一复验点仍持续；必须由 connection-reported canonical filename + pathname identity 多点绑定拒绝。若当前 OS 用户在所有复验点之间完成 A→B→A，则属于明示 P2/TCB 限制，不伪称当前标准库实现已拥有 VFS fd 证明。
- cold database 含一个额外 trigger，它在合法 receipt insert 时写入额外表；必须在 schema allowlist 或 exact DML successor 处拒绝。
- 旧 non-frontier operation 自洽改写后，另一 repository 又合法 append 一个新 operation；原 repository 不得因 frontier 合法前进而接受已重写的 prefix。
- managed child 忽略 `SIGTERM`，或 `kill()` 返回 false，或 `SIGKILL` 后仍没有 exit/close；替代 spawn 与 app final quit 必须等待可证明的 termination result。
- health response 伪造过大/illegal `Content-Length`、不带长度的 8,193-byte stream 或超深 JSON；均必须在建立 trust 前有界失败。
- 同名 index 被重建为不同列、`ASC/DESC` 或 predicate；即使 `(type,name,tbl_name)` 未变，normalized SQL 不同也必须在 mutation 前拒绝。
- 另一 project 写入 32 条 paired receipt/capability history，当前 project 在该表无新行；当前 project 的 cold lease 仍必须记住 global high-water，下一次 warm query 只读取空的新 rowid interval。
- attacker 以一次 unrelated `UPDATE` 替换预期 `creative_projects.updated_at` touch，或在同一 timestamp/change count 下改写 title/status；immutable vector 未变也不得接受，typed mutable state/head/operation timestamp 必须 exact match。
- 外部 DBA 执行 `ANALYZE` 产生 `sqlite_stat1`/`sqlite_stat4`；下一次 Blueprint command 在 callback 前 fail closed，不自动 drop 统计表。

## Success Criteria

- **SC-B1A-001**：同一 repository 的 100 次健康 append 中 full historical audit 次数恰为 1；之后每次只验证一个 delta。
- **SC-B1A-002**：warm commit/no-change/restore 在 10、50、100、1000 revision fixture 上历史 row decode 数为 0，SQL statement 数不随 R 增长；wall-clock 只作辅助证据。
- **SC-B1A-003**：operation、revision、head、desktop/agent receipt、sidecar、capability fact/use event 的外部篡改矩阵中，下一次 command 错误走 fast path 的次数为 0，完整审计检出率 100%。
- **SC-B1A-004**：rollback/commit fault 与外部合法 writer race 中错误发布 lease 数为 0；新 repository 冷审计率 100%。
- **SC-B1A-005**：pairing/revoke/decision never-resolving fetch 与 body 共至少 4 个 oracle 均在 12 秒 deadline 模型下返回稳定错误，native review/mutation 数为 0，timer/body reader 泄漏数为 0。
- **SC-B1A-006**：credential loader 的 path-level full read 数为 0；同 fd open/fstat/read、symlink、permission、FIFO replacement 非阻塞、16,384/16,385 byte 边界测试全部通过。
- **SC-B1A-007**：全量 `npm run check`、dependency audit 与独立只读安全/架构审计通过；P0/P1 为 0 后才可标记 implemented/validated。
- **SC-B1A-008**：受影响、修复 backport、3.51.3+ 与 unknown SQLite version oracle 均命中正确 admission；受影响版本的 anchor 创建数为 0，Desktop writer 在 0 DB connect/0 spawn 前 fail closed。
- **SC-B1A-009**：health capability 缺失/false/malformed 即使 HMAC 正确也被拒绝；safe capability 正常启动，payload 不含 executable、DB path 或 app-state path。
- **SC-B1A-010**：旧 unsafe `.venv` 会被 setup 重建；Core/Desktop safe-runtime lane 与 Python 3.10 plugin-only lane 分别通过。
- **SC-B1A-011**：direct/base-descriptor/cursor/`executescript` 事务逃逸 oracle 检出率 100%；任一 partial-commit 可能路径后的第二次 Blueprint command 成功数为 0。
- **SC-B1A-012**：warm 与 unsafe-runtime fallback 的 replay/no-change/new-revision/restore 都执行 exact successor validator；额外 DML 和额外 trigger side effect 放行数为 0。
- **SC-B1A-013**：额外 main schema object、temp object、attached database 和 normalized definition mismatch 全部在 command commit 前被拒绝。
- **SC-B1A-014**：connection-reported canonical filename mismatch、repository pathname identity 在任一复验点持续改变、same-UUID clone 持续替换的 oracle 放行数为 0；文档与错误不宣称高于这一证据等级的 VFS opened-fd proof。
- **SC-B1A-015**：旧 non-frontier operation/revision/receipt 被重写后再合法 append 的矩阵检出率 100%；未被改写的合法 external append 接受率 100%。
- **SC-B1A-016**：`SIGTERM`、`SIGKILL`、kill-false、ignored-signal 与 stop-then-immediate-ensure oracle 中，同时存活的 managed backend root process 最大数为 1。
- **SC-B1A-017**：debug 启动的 Flask reloader 子 writer 数为 0；Electron `before-quit` 只在 managed termination 被确认后进入最终 quit。
- **SC-B1A-018**：health 8,192/8,193-byte 声明与 streaming 边界、JSON depth 32/33 与括号-in-string oracle 全部通过。
- **SC-B1A-019**：同 identity/不同 SQL 的 schema replacement 矩阵在 mutation 前拒绝率 100%。
- **SC-B1A-020**：10/50/100/1000 warm fixture 的 SQL statement count 与 `EXPLAIN QUERY PLAN` shape 相等；10 张 immutable table 的 incremental query 全部使用 bounded rowid range，full scan/temp B-tree 数为 0。
- **SC-B1A-021**：safe/fallback 中 equal-count unrelated DML 与 corrupted `creative_projects` row substitution 放行数为 0；replay/no-change/new-revision 的 immutable+mutable expected delta 全部精确匹配。
- **SC-B1A-022**：另一 project 写入 32 条 paired history 后，当前 project 对应表的 frontier 等于 global `MAX(rowid)`；下一次 warm query 不重扫描旧跨项目区间。
- **SC-B1A-023**：当前 final Core diff 的 B1A 42/42、B1 相关 78/78、Blueprint API/contract/state-machine 28/28、plugin 228/228 通过；focused Ruff 与 diff-check 通过，独立 Core 复审 P0=0/P1=0。该成功标准仅是 local validation，不等于 remote CI。

## Assumptions and Dependencies

- SQLite 当前仍是本地单用户数据库；当前 OS 用户属于 B1 threat model 的 TCB。
- SQLite `PRAGMA data_version` 只用于检测同一 observer connection 之外的 commit；不能替代内容审计，也不作为持久版本号。
- 当前仓库 `.venv` 的 SQLite 版本必须作为 release evidence 实测，不能从 Python 主版本推断；若仍为受影响版本，B1A 性能能力不得宣称默认启用。
- 依赖 B1 已有 immutable tables/triggers、permanent receipts、CAS、strict JSON、resource budget 与 full validators。
- managed schema 采用 closed-world 语义；通用 DBA 优化动作（尤其 external `ANALYZE`/`sqlite_stat*`、`VACUUM` 或 schema rebuild）在未有专用 maintenance 合同前不属于支持操作。
- normalized SQL 是保守的 physical identity；语义等价但文本归一化结果不同的 DDL 可能被误拒绝，该 availability trade-off 属于已知 P2。
- `VACUUM`/重建可改变 rowid、rootpage 或数据库 pathname identity，因此现有 repository 保守 poison/fail-close 是预期行为，不构成自动恢复承诺。
- exact mutable successor 要求新 operation/head/project update 的 timestamp 关系可区分；如果时钟生成器给出与前一 project `updated_at` 完全相同的值，command 会保守拒绝，作为 availability P2 而非放宽 integrity 合同。

## Implementation Authorization

授权仅覆盖本 Spec、测试与上述 B1A runtime/integrity hardening 改动。不得借此实施 B2、修改产品 authority 语义、增加外网或原文件副作用。
