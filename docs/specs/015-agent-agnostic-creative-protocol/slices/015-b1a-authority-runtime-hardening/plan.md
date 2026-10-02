# Implementation Plan: ML-015-B1A Authority Runtime Hardening

- 实现状态：`IMPLEMENTED`
- 验证状态：`LOCALLY VALIDATED`
- Remote CI：`NOT RUN`
- 本地仓库 gate：`PASSED`
- 发布/Remote CI gate：`PENDING / NOT RUN`
- 原则：先用失败 oracle 证明缺口，再实现最小闭环；不改变 V5 schema 或授权面。

## Phase 1 — Contract and Oracles

1. 冻结 Spec、stable errors、性能/篡改 oracle 与 B1/B2 边界。
2. 增加 credential single-fd/bounded-read 失败测试。
3. 增加 Electron never-resolving fetch/body、no-review/no-mutation、timer cleanup 失败测试。
4. 增加 warm/cold audit counter、external data-version invalidation、rollback/no-publish、1000-revision slope 失败测试。
5. 增加 callback transaction-escape、same-UUID clone/persistent path mismatch during connection lifetime、extra cold schema/trigger side effect、non-frontier rewrite followed by legitimate append 四类独立审计 oracle。
6. 增加 backend kill-false/ignored-signal/SIGKILL/stop-then-immediate-ensure、debug process-tree、health 8 KiB 与 strict JSON lexical-depth oracle。

## Phase 2 — Independent Runtime Hardening

1. credential loader 改为 `os.open`、`O_NOFOLLOW`、`fstat`、identity comparison 与 16,385-byte read。
2. Electron request primitive 增加 12 秒 controller/deadline，并覆盖 bounded body reader。
3. 聚焦测试与 lint/typecheck 通过后，不提前宣称整片完成。

## Phase 3 — Verified-Prefix Lease

1. 在 `MediaRepository` 内增加 bounded LRU lease、RLock 与长寿命 SQLite observer；提供明确 close/cleanup。
2. 冷路径复用原 full validators建立 exact prefix；快路径只由内部 transaction context开启。
3. 为 current head、historical restore target 与本次 delta 提供有界读取/验证，不公开 bypass flag。
4. desktop 与 paired Blueprint command 迁移到 observer-owned transaction；其他写触发 data-version invalidation 即可。
5. commit 后原子发布新 lease；rollback、异常、observer change 清除候选。

## Phase 3A — Safe WAL Writer Admission

1. 建立单一 SQLite version/capability 判据与 CLI checker，覆盖 backport、withdrawn 与 unknown 分支。
2. setup/prepare/run scripts 依据实际链接的 SQLite 选择或重建 managed venv，不从 Python 主版本推断安全性。
3. backend 在任何 app state/DB 副作用前 fail closed；health 只公开最小 runtime capability。
4. Electron health trust 与 auto-start preflight 同时验证 capability；unsafe/malformed/timeout 均 0 spawn。
5. CI 分离 safe Core/Desktop writer lane 与 Python 3.10 plugin reader compatibility lane。

## Phase 3B — Independent Audit Closure

1. 在 yielded callback 前后建立不可由 caller 伪造的 transaction-continuity proof；authorizer 与 connection override 只作阻止/纵深防御，不代替连续性证明。疑似 partial commit 后 poison repository。
2. warm 与 fallback 都建立真实 `_VerifiedPrefixTransaction`，复用同一 exact DML/successor validator；不得用 `context=None` 绕过。
3. 冻结 canonical V5 `sqlite_schema` allowlist，cold audit 先拒绝额外 table/index/trigger/view；command 前后仍比较 exact schema snapshot。
4. 把 versioned historical-prefix digest 纳入 process-local lease/floor。cold audit 计算全 prefix，warm successor 用一个已验 leaf 延伸，external append 后重算旧 floor 并比较。
5. 将 pathname admission 收紧为 same-connection binding：`PRAGMA database_list` 的 canonical main filename 必须等于 repository path，并在 begin/audit/pre-commit/post-commit 多点复验 repository-lifetime pathname identity、UUID 与 schema。不使用 `ctypes` 依赖 CPython 私有 `_sqlite3` ABI，不宣称 VFS opened-fd inode proof；复验点间的 same-user A→B→A rename 显式留为 P2/TCB 边界。

## Phase 3C — Backend Lifecycle and Bounded Transport

1. 将 managed process stop 改为 single-flight confirmed-termination barrier：`SIGTERM` 有界等待，必要时 `SIGKILL` 再等待，失败则保留 ownership 并拒绝 replacement。
2. `before-quit` 等待 confirmed stop；用 startup generation 与 process identity 阻止旧 event 覆盖新 ownership。
3. backend debug 固定 `use_reloader=false`，动态 process-tree oracle 证明只有一个 writer root。
4. health reader 对 `Content-Length` 和实际 stream 同时施加 8,192-byte 上限；strict HTTP JSON 在 decoder 前完成 32 层 lexical-depth 预检。

## Phase 3D — Second Independent Core Audit Closure

1. 将 cold schema allowlist 从 `(type,name,tbl_name)` 升级为 `(type,name,tbl_name,normalized_sql)`，用同 identity/不同 DDL 反例证明 mutation 前 fail closed。
2. 将 warm prefix SQL 收紧为 global rowid 双边区间；用 `EXPLAIN QUERY PLAN` 固定 integer-primary-key range，拒绝 full scan 与 temp B-tree。
3. 将 exact delta 从单一 `total_changes` 扩展为 10-table immutable successor vector + 完整 typed `creative_projects` state + head/operation timestamp binding + total-change conservation，并拒绝 equal-count substitution。
4. 每表 commitment 记录 global `MAX(rowid)`，包括当前 project 命中 0 行的区间；以 32 条 cross-project paired history 证明下一次 warm command 不重扫描旧区间。
5. 把 external `ANALYZE`/`sqlite_stat*`、`VACUUM` 与 schema rebuild 写入 closed-world 运维合同：不自动修复，变化后 fail closed 并要求可验备份/受支持工具恢复。

## Phase 4 — Gates and Evidence

1. 跑 focused Core/plugin/Electron tests、ruff、typecheck/build。
2. 跑 10/50/100/1000 warm slope 与 cold/tamper/fault matrix。
3. 跑 transaction-escape、connection canonical-filename/path-identity mismatch、exact-schema/delta 和 historical-prefix rewrite 对抗矩阵。
4. 跑 backend termination/process-tree、health 8 KiB 与 lexical-depth 边界矩阵。
5. 跑两次仓库级 `npm run check` 与 dependency audit。
6. 独立只读审计实现、测试和文档；只在证据齐全后更新状态、README/CHANGELOG/version。本地通过不得记作 remote CI 已运行。

最终本地证据：B1A 42/42、B1 相关 78/78、Blueprint API/contract/state-machine 28/28、plugin 228/228、Ruff/diff-check 通过，独立 Core 复审 P0=0/P1=0；最终 diff 上两轮仓库级 `npm run check` 均通过 Core/unit 289、plugin 228、Node combined 86、renderer models 46，并完成 verify/build，dependency audit 为 0 个已知漏洞。版本与父文档已收口为 0.10.1 local candidate；remote CI、tag 与发布仍未运行。

## Rollback

- Electron/credential 是独立代码路径，可单独回退。
- verified-prefix lease 是纯 runtime optimization；删除 lease/context 后 Core 回到现有 full-audit 行为，不需要数据库降级。Writer runtime admission 不得因回退 optimization 而关闭。
- transaction continuity、connection/path binding、exact cold schema/delta 和 historical-prefix proof 是 correctness boundary，不是可单独关闭的性能功能。如果无法证明当前合同，必须拒绝 Blueprint authority 写入，不能以旧 fallback 作为降级。
- normalized schema SQL、bounded rowid plan、immutable+mutable successor vector 与 global high-water 也是 correctness/availability boundary；回退不得恢复到“同名对象即可信”、无界扫描或只数 change count 的实现。
- 任何无法解释的 observer/lease 状态必须选择丢弃 cache，而不是保留快路径。
