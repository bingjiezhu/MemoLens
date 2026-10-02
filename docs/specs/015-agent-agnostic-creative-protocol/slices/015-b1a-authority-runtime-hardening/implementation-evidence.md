# Implementation Evidence: ML-015-B1A Authority Runtime Hardening

- 证据快照：2026-08-22（两轮共八个 Core P1 关闭、独立二次复核与最终本地仓库 gate 后）
- 实现状态：`IMPLEMENTED`
- 验证状态：`LOCALLY VALIDATED`
- Remote CI：`NOT RUN`
- 本地仓库 gate：`PASSED`
- Release/Remote CI：`NOT RUN`
- schema/capability 边界：V5；无 B2 compiler/render/export；MCP `write=false`
- 证据规则：只记录当前 checkout 上实际观察的实现、命令和结果。早于当前 diff 的绿色结果会标为 stale/pre-final，不得当作最终发布证据。

## 1. Implemented surfaces

| Contract | Current implementation surface | Current evidence boundary |
| --- | --- | --- |
| 12-second authority deadline | `electron/agentAuthorityCoordinator.ts` | fetch 和 bounded response-body read 共用 deadline；timeout 后 presentation 不进入 native review/mutation |
| CLI same-fd credential read | `.agents/plugins/plugins/memolens/scripts/memolens_agent_credentials.py` | `os.open` + 平台可用的 `O_NOFOLLOW/O_NONBLOCK` + 同 fd `fstat/read`；最多读 16,385 bytes |
| Single SQLite runtime policy | `core/sqlite_runtime.py`, `core/sqlite_runtime_policy.json`, `scripts/check_sqlite_runtime.py` | backport/withdrawn/unknown 版本由同一 policy 判定；不从 Python 主版本推断 |
| Isolated writer admission | `scripts/run_python.sh`, `scripts/bootstrap_mac.sh`, `scripts/prepare_desktop_runtime.sh`, `backend/app.py`, `backend/src/__init__.py` | writer 以 `python -I` 运行；unsafe/unknown SQLite 在 app/DB/directory/spawn 前 fail closed |
| SQLite-attested health | `backend/src/api/routes.py`, `electron/backendManager.ts`, `electron/backendProcessSupervisor.ts` | exact health shape + session HMAC + exact safe runtime capability；payload 不公开 executable/DB/app-state path |
| Health 8 KiB bound | `electron/backendManager.ts` | 检查 `Content-Length`，并对 actual streamed bytes 独立计数；8,193 bytes 拒绝 |
| Strict HTTP JSON | `backend/src/api/strict_json.py` | 读取上限 1 MiB；decoder 前 string-aware lexical depth=32；后续 duplicate/resource/canonical limits 不变 |
| Confirmed backend termination | `electron/backendProcessSupervisor.ts`, `electron/backendManager.ts`, `electron/main.ts` | stop/retry 单飞；`SIGTERM` 后 `SIGKILL`；未确认 exit/close 时保留 ownership 且拒绝 replacement；quit 等待 stop |
| Single Flask writer root | `backend/app.py` | debug 不改变 `use_reloader=False` |
| Verified-prefix optimization | `core/media_db.py` | bounded 16-project LRU、process/PID reset、observer `data_version`、warm exact successor、cold exact-schema/full-audit fallback、10-table immutable-prefix commitments 与 SQLite safe-runtime gate |
| Connection/path binding remediation | `core/media_db.py` | same-connection `PRAGMA database_list` canonical main filename + repository pathname identity/UUID/schema 多点复验已通过 local oracle 与独立二次复核 |
| Normalized closed-world schema | `core/media_db.py` | cold allowlist 精确比较 `(type,name,tbl_name,normalized_sql)`，command 前后仍保留 exact physical snapshot |
| Bounded rowid execution plan | `core/media_db.py` | 每表使用 global high-water 和 `(old,new]` rowid range；`EXPLAIN QUERY PLAN` oracle 证明 integer-primary-key range，无 full scan/temp B-tree |
| Exact immutable + mutable successor | `core/media_db.py` | 10-table delta vector + typed `creative_projects` state + head/operation timestamp + `total_changes` conservation，拒绝 equal-count substitution |
| Cross-project high-water | `core/media_db.py` | 每表 commitment 保存 global `MAX(rowid)`；0-hit interval 也前进，不重扫描其他 project 历史 |

上述 Core 实现已经过当前 checkout 的本地测试和独立二次复核。这些证据不能扩张为 remote CI、hosted validation 或 released 声明。

## 2. Focused validation observed

| Scope | Observed result | Admission status |
| --- | --- | --- |
| Electron backend manager/supervisor focused suite | 22/22 passed；含 ignored signal、kill-false、SIGKILL escalation、stop-then-immediate-ensure | 可作 backend lifecycle focused 证据 |
| Electron authority coordinator focused suite | 23/23 passed；含 pairing/revoke/decision stalled fetch/body 与 no-review/no-mutation | 可作 authority deadline focused 证据 |
| SQLite setup/admission focused suite | 14/14 passed；包含 `use_reloader=False` 静态合同 | 可作 setup/reloader focused 证据 |
| Electron build/typecheck | passed | 可作 focused TypeScript 结构证据；最终 diff 已由下列两轮整仓 gate 独立重跑 |
| Dynamic debug process-tree oracle | 只观察到一个 `backend/app.py` root | 可作 reloader-off focused 证据 |
| Backend independent re-audit | `GO`, P0=0, P1=0 | 覆盖 backend/runtime 子域；Core 证据由下列独立 rows 给出 |
| Python 3.10 plugin reader CI-equivalent lane | 181/181 passed | 证明 plugin Python 3.10+ 边界未被 writer admission 抬高；不等于 remote CI |
| `tests.test_b1a_verified_prefix` | **42/42 passed** | **current Core local evidence**；覆盖两轮 8 个 P1、10/50/100/1000 constant statement/plan shape、global high-water 与 32-row cross-project oracle |
| B1 ledger/persistence/authority/backend suites | **78/78 passed** | **current Core local evidence** |
| Blueprint API/contract/state-machine suites | **28/28 passed** | **current Core local evidence** |
| Full plugin suite | **228/228 passed** | **current local evidence**；不等于 remote CI |
| Core Ruff + diff-check | **passed** | **current Core local evidence** |
| Independent Core second review | **GO, P0=0, P1=0** | 准入当前 Core local implementation；不得扩张为整仓 release/remote-CI 结论 |
| First repository-wide `npm run check` | passed；当时 discover 统计为 Core/unit 279、plugin 228，Node combined 86，renderer models 46 | **pre-final/stale**；早于两轮 8 个 Core P1 最终修复，不计入 T013 要求的两次 final check |
| Final repository-wide `npm run check` — run 1 | **passed**；Core/unit 289、plugin 228、local verify、renderer/Electron build、Node combined 86、renderer models 46 | **current final-diff local evidence** |
| Final repository-wide `npm run check` — run 2 | **passed**；Core/unit 289、plugin 228、local verify、renderer/Electron build、Node combined 86、renderer models 46 | **current final-diff local evidence**；与 run 1 独立完整执行 |
| Final dependency audit | **passed**；`npm audit --audit-level=high` 为 0 vulnerabilities，`pip-audit` 为 no known vulnerabilities | **current local dependency evidence**；不等于未来时点或 remote runner 的持续保证 |

当时 focused/full 运行使用了临时 safe interpreter：Python 3.14.2 arm64 + SQLite 3.53.4。仓库旧 `.venv` 实测为 Python 3.11.11 + SQLite 3.47.1，会按合同退出 78；它只有在 setup 重建并重新 probe 后才能成为 Desktop writer runtime。

## 3. Eight Core P1 findings and local closure

两轮独立 Core 复核共发现 8 个 P1。当前实现已命中对应 red-team oracle，并以 B1A 42/42、B1 相关 78/78、Blueprint 28/28、plugin 228/228、Ruff/diff-check 与独立二次复审 P0=0/P1=0 收口。

### First review

1. **Transaction escape**：原反例可在 operation/revision/head 后自行 commit 再抛异常。修复使用随机 savepoint 证明 outer transaction 连续性，并以 authorizer + `_MediaConnection` override 作阻止/纵深防御；base-class 绕过导致 savepoint 丢失时返回 `blueprint_post_commit_verification_indeterminate` 并 poison repository。
2. **Connection/path binding TOCTOU**：原实现只在 connect 前单点 `stat`。修复绑定 same-connection `PRAGMA database_list` canonical main filename，并在 open/BEGIN/pre-commit/post-commit 多点复验 pathname identity/UUID/schema；persistent same-UUID clone 在 mutation 前返回 `blueprint_database_binding_indeterminate`。未使用 `ctypes` 私有 ABI，未宣称 VFS fd/inode proof。
3. **Cold schema/fallback delta gap**：修复对完整 managed V5 schema object set 做 exact allowlist，拒绝额外 table/index/trigger/view；fallback 也创建 `_VerifiedPrefixTransaction`、复用 exact DML/successor validator，并在 mutation 后再做 full audit。
4. **Frontier-only floor**：修复对 10 张 immutable project table 保存 rowid-bounded rolling commitments。cold external extension 重验旧 commitments，warm command 只对有界新行延伸；旧 sequence-1 改写后再合法 append sequence-3 的 oracle 在 mutation 前 poison。

### Second review

5. **Schema object identity omitted SQL**：原 exact allowlist 只比较 `(type,name,tbl_name)`，同名但定义已改的 index 仍可通过。修复比较 `(type,name,tbl_name,normalized_sql)`，同 identity/不同 SQL replacement 在 mutation 前拒绝。
6. **Constant SQL count omitted execution cost**：原 slope oracle 只数 SQL statements，不能排除每条扫描全历史。修复以 global rowid 双边 range 约束查询，并用 `EXPLAIN QUERY PLAN` 证明 10 表全部使用 integer-primary-key range，无 full scan/temp B-tree。
7. **Equal-count successor substitution**：只校验 `total_changes` 可以用 unrelated `UPDATE` 替换 project touch，或在相同 count 下改坏 mutable row。修复将 10-table immutable delta vector、完整 typed `creative_projects` state、head/operation timestamp 与 `total_changes` 守恒同时验证；safe/fallback equal-count 攻击全部回滚。
8. **Project-local high-water rescanned cross-project history**：仅保存当前 project 最大 rowid 会在每次 warm command 重扫描其他 project 历史。修复使每表 frontier 等于 global `MAX(rowid)`，0-hit interval 也前进；32 条 cross-project paired history 后的下一次 warm query 仅观测新 global interval。

该结论连同下列双轮仓库级 gate，只准入当前 checkout 的 0.10.1 local candidate；不代替 remote CI、tag、release publication 或未来依赖状态。

## 4. Final local gates

- [x] T012B–T012E 与 T012G–T012J 两轮共八个 Core P1 都有对抗 oracle 与实现修复。
- [x] 当前 Core local suites：B1A 42/42、B1 相关 78/78、Blueprint 28/28、plugin 228/228、Ruff/diff-check 通过。
- [x] 10/50/100/1000 statement/plan slope、tamper/fault、transaction escape、persistent clone/path mismatch、normalized schema、equal-count substitution、global high-water 与 historical rewrite 矩阵通过。
- [x] 独立 Core 二次复核结论 `P0=0 / P1=0 / GO`。
- [x] 最终 diff 上连续两次 `npm run check` 通过；两轮计数均为 Core/unit 289、plugin 228、Node combined 86、renderer models 46，并完成 local verify 与 renderer/Electron build。
- [x] 最终 diff 上 dependency audit 通过；npm 与 Python 均为 0 个已知漏洞。
- [x] README/CHANGELOG/parent indexes/version 已按真实最终状态收口为 0.10.1 local candidate，并保留 0.10.0 历史证据。
- [ ] remote GitHub Actions CI 运行并留下对应 commit/diff 证据。

## 5. Remote CI status

**Not run.** 本次只有本地与 CI-equivalent 证据，没有 GitHub-hosted runner 结果。因此不得宣称“CI 已通过”；只能说明哪些本地 lane 曾通过。

## 6. Closed-world operations

- 不要对活跃 MemoLens database 手工运行 `ANALYZE`。它可创建 `sqlite_stat1`/`sqlite_stat4`，这些对象不在 managed V5 allowlist 中；下一次 Blueprint authority command 将在 mutation 前以 `blueprint_schema_integrity_error` fail closed。
- 不要手工创建、删除或编辑 `sqlite_stat*`，也不要对活库执行未受支持的 `VACUUM`/schema rebuild。closed-world checker 不会自动删除这些对象或重写数据库。
- 如外部 maintenance 已发生，停止 Blueprint 写入，保留 database + WAL 证据，从可验证备份恢复，或等待明确支持该变化的 maintenance/repair 工具。不得通过直接编辑 SQLite 内部表来绕过检查。

## 7. Residual P2 and bounded limitations

下列项目是八个 Core P1 已关闭后的非阻断 residual；不得用它们降级新发现的 P0/P1：

- Python 3.14 focused/full 运行仍观察到多个 unclosed SQLite connection `ResourceWarning`；目前未观察到数据错误，但应后续收紧 test/fixture cleanup。
- historical-prefix floor 只对当前 repository/process lifetime 有效；crash/restart 后重新 full audit，不提供跨进程 anti-rollback。
- warm lease 的 LRU 上限是 16 个 project，但 process-local attested floors 的内存集合仍按 repository 历史项目增长；需要后续的容量/生命周期策略。
- `python -I` 去除 `PYTHONPATH/PYTHONHOME/user-site` 注入，但 managed venv 内已安装 package 仍属 writer TCB。
- Electron preflight 的 executable stat/realpath 到 spawn 之间仍存在很小的 same-user TCB 窗口；本切片不提供对当前 OS 用户的密码学隔离。
- Core database binding 使用 same-connection canonical filename + pathname `(st_dev, st_ino)` 多点复验。标准库 `sqlite3` 不暴露 VFS fd，因此当前 OS 用户在所有复验点间精确完成 A→B→A rename 的极窄窗口保留为 P2/TCB 限制；未使用 `ctypes` 破解 CPython 私有 ABI。
- normalized SQL 采用保守 physical identity；语义等价但 normalized text 不同的 DDL 会被误拒绝。这是 availability P2，不会放宽为接受未知 definition。
- external `VACUUM` 或 schema rebuild 可改变 rowid、rootpage 或 pathname identity；现有 repository 会保守 fail closed/poison，而不尝试猜测语义等价。
- exact mutable successor 要求新 operation/head/project update 的 timestamp 关系可区分。若时钟生成器返回与上一个 project `updated_at` 完全相同的值，该 command 会保守拒绝；这是 availability P2。
- backend supervisor 目前证明/管理的是受管 root child；未提供通用跨平台 descendant process-tree kill 合同。debug reloader off 关闭了当前已知的第二 writer 来源。
- 仓库旧 `.venv` 仍是 unsafe runtime；该状态被 admission 正确阻断，但用户仍需要执行 setup 重建才能使用 Desktop writer。

## 8. Evidence-to-requirement map

| Evidence family | Requirements |
| --- | --- |
| authority deadline + no-review/no-mutation | FR-B1A-009–011, SC-B1A-005 |
| credential same-fd + 16,384/16,385 boundary | FR-B1A-012–014, SC-B1A-006 |
| SQLite policy/admission/health | FR-B1A-016–021, SC-B1A-008–010 |
| transaction/atomic closure | FR-B1A-022–023, SC-B1A-011–012 |
| exact cold schema + connection/path binding | FR-B1A-024–025, SC-B1A-013–014 |
| historical prefix proof | FR-B1A-026, SC-B1A-015 |
| backend termination + reloader | FR-B1A-027–028, SC-B1A-016–017 |
| bounded health + lexical depth | FR-B1A-029–030, SC-B1A-018 |
| normalized schema SQL + bounded rowid plan | FR-B1A-031–032, SC-B1A-019–020 |
| exact immutable/mutable successor + global high-water | FR-B1A-033–034, SC-B1A-021–022 |
| closed-world SQLite maintenance | FR-B1A-035 |
| final Core local validation | SC-B1A-023 |
