# Tasks: ML-015-B1A Authority Runtime Hardening

- 实现状态：`IMPLEMENTED`
- 验证状态：`LOCALLY VALIDATED`
- Remote CI：`NOT RUN`

`[x]` 只表示该任务在当前 checkout 已实现且有 local evidence；不代表 released 或 remote-CI validated。两轮独立 Core 审计的共八个 P1 由 T012B–T012E 与 T012G–T012J 跟踪，当前均已关闭并经独立本地复审 P0=0/P1=0。T013 的本地整仓 gate 与 T015 的版本/父文档收口已完成；remote CI、tag 与 release publication 仍不得推定为完成。

- [x] T001 冻结 B1A scope、authority boundary、stable errors 与非目标。
- [x] T002 写 credential same-fd/O_NOFOLLOW/fstat/bounded-read 失败测试。
- [x] T003 实现 credential single-fd reader 并通过 focused tests/ruff。
- [x] T004 写 Electron 12 秒 deadline、never-resolving fetch/body 与 no-mutation 测试。
- [x] T005 实现 Electron shared authority deadline 并通过 focused Node/build/typecheck。
- [x] T006 写 verified-prefix warm/cold/full-audit counter 与 10/50/100/1000 slope oracle。
- [x] T007 写 external SQLite tamper/legitimate commit、新 repository、UUID/schema/path invalidation oracle。
- [x] T008 写 rollback/commit fault、replay、no-change、restore、paired capability-use delta oracle。
- [x] T009 实现 bounded lease、observer connection、private fast-path context 与 cleanup。
- [x] T010 实现 warm current/restore-target/delta validation，不删除现有 cold validators。
- [x] T011 将 desktop/paired Blueprint command 接入 verified transaction，并证明 authority/MCP 边界不变。
- [x] T012 增加 SQLite 单一 safe-version/capability 判据；unsafe/unknown runtime 的 Core anchor 为 0，Desktop/backend writer 在任何 DB/spawn 前 fail closed。
- [x] T012A 接入 setup/prepare/run scripts、health/Electron preflight 与 safe-writer/plugin-reader 分离 CI。
- [x] T012B 关闭 callback/extension transaction escape；证明 operation/revision/head/receipt/capability-use 只能在一个连续事务中成功，partial-commit 可能时永久 poison repository。
- [x] T012C 将 database admission 绑定到 same-connection `PRAGMA database_list` canonical filename + repository pathname identity 多点复验；增加 persistent same-UUID clone/path mismatch 在 connect/audit/pre-commit/post-commit 窗口的对抗 oracle。不使用 `ctypes` 私有 ABI，不宣称 VFS fd/inode proof。
- [x] T012D 冻结 cold V5 exact schema allowlist；fallback 创建真实 successor context，拒绝额外 DML、额外 trigger 与“改回去”副作用。
- [x] T012E 对 10 张 immutable project table 的整段已验历史建立 versioned rolling commitments；在 external legitimate append 后仍检出旧 non-frontier rewrite。
- [x] T012F 实现 Electron confirmed-termination barrier、`before-quit` 等待、debug reloader off、health 8 KiB bound 与 strict JSON lexical-depth precheck，并通过 focused 测试。
- [x] T012G 将 cold schema exact allowlist 扩展为 `(type,name,tbl_name,normalized_sql)`，拒绝同 identity/不同 SQL 的 index/trigger/view/table replacement。
- [x] T012H 将 10 表 warm prefix query 收紧到 global rowid 双边区间，并用 `EXPLAIN QUERY PLAN` 证明 integer-primary-key range、无 full scan/temp B-tree。
- [x] T012I 增加 exact 10-table immutable delta vector、typed `creative_projects` mutable state、head/operation timestamp binding 与 `total_changes` 守恒，拒绝 equal-count unrelated/corrupt substitution。
- [x] T012J 将每表 frontier 改为 global `MAX(rowid)`，对 0-hit interval 也推进；通过 32 条 cross-project paired-history oracle。
- [x] T012K 记录 external `ANALYZE`/`sqlite_stat*`、`VACUUM` 与 schema rebuild 的 closed-world fail-closed 运维规则。
- [x] T013A 跑当前 Core local gates：B1A 42/42、B1 相关 78/78、Blueprint API/contract/state-machine 28/28、plugin 228/228、Ruff 与 diff-check。
- [x] T013 跑并单独记录两次仓库级 final check 与 final dependency audit；两轮 `npm run check` 均为 Core/unit 289、plugin 228、Node combined 86、renderer models 46，dependency audit 为 0 个已知漏洞。
- [x] T014 独立 Core 安全/架构二次复审；当前结论 P0=0/P1=0，非阻断 P2 已记录。
- [x] T015 更新 implementation evidence、README、CHANGELOG、父级 Spec 索引与 patch version；保留 0.10.0 历史事实，并把 0.10.1 限定为 local candidate。
