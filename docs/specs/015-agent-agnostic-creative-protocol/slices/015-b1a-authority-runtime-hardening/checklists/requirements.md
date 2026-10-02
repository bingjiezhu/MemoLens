# Requirements Checklist: ML-015-B1A

- 实现状态：`IMPLEMENTED`
- 验证状态：`LOCALLY VALIDATED`
- Remote CI：`NOT RUN`

## Scope and Authority

- [x] 不扩展产品能力；关闭 B1 已记录的三个确定缺口、SQLite writer blocker 与两轮独立 Core 审计阻断项。
- [x] MCP 保持只读，Agent semantic confirm/revoke 仍为 0。
- [x] 不进入 compiler/render/export/file-operation/B2。
- [x] 不新增 V6 schema 或把同库 checkpoint 冒充信任根。

## Integrity Fast Path

- [x] 冷启动与外部 change 明确要求 full audit。
- [x] fast path 没有 caller-controlled bypass。
- [x] lease/process/connection/database/schema/project/head/frontier binding 已定义。
- [x] rollback 与 observer race 的 publication rule 已定义。
- [x] restore/no-change/replay/paired-use delta 均在验收范围。
- [x] 基础 fast-path 失败 oracle 与 implementation 已建立。
- [x] 基础 10/50/100/1000 deterministic slope 与 tamper matrix 至少一次 focused 通过。
- [x] SQLite WAL-reset/data-version官方边界已纳入设计。
- [x] unsafe/unknown SQLite runtime 的 anchor admission 为 0；Desktop writer 在 0 DB connect/0 spawn 前 fail closed 的 focused oracle 已通过。
- [x] SQLite 判据只有一个来源，health 与 Electron 不复制版本阈值。
- [x] safe Core/Desktop writer focused lane 与 Python 3.10 plugin reader CI-equivalent lane 已各至少通过一次。

## Independent Core Audit Closure

- [x] transaction escape、same-UUID clone、extra schema/fallback side effect、non-frontier rewrite 四类反例已被识别并写入 Spec。
- [x] callback 无法用 direct transaction control 伪造最终事务状态；base-class 绕过造成 savepoint 丢失时返回 `blueprint_post_commit_verification_indeterminate` 且 repository 被 poison。
- [x] success 必须与 operation/revision-or-no-change/head/receipt/capability-use 同事务原子闭包。
- [x] same-connection `PRAGMA database_list` canonical filename + repository pathname identity/UUID/schema 多点绑定已通过 persistent clone/path-mismatch oracle；无 `ctypes` 私有 ABI，无 VFS fd/inode proof 夸大声明。
- [x] cold `sqlite_schema` 对象集为 exact V5 allowlist，额外 table/index/trigger/view/temp/attachment 被拒绝。
- [x] fallback 复用 exact successor/delta validator，额外 DML 与 trigger side effect 放行数为 0。
- [x] process-local floor 以 10 张 immutable project-table rolling commitments 证明整段 historical prefix；external legitimate append 后仍能检出旧 non-frontier rewrite。

## Second Core Audit Closure

- [x] cold allowlist 比较 `(type,name,tbl_name,normalized_sql)`，同 identity/不同 DDL 在 mutation 前拒绝。
- [x] 10 表 warm query 使用 global rowid 双边 range，`EXPLAIN QUERY PLAN` 无 full scan/temp B-tree。
- [x] successor 同时验证 exact immutable delta vector、typed mutable project state、head/operation timestamp 与 total-change conservation。
- [x] safe/fallback equal-count unrelated DML 和 corrupted `creative_projects` substitution 放行数为 0。
- [x] 每表 frontier 记录 global high-water，0-hit interval 也推进；32 条 cross-project paired history 后不重扫描旧区间。
- [x] external `ANALYZE`/`sqlite_stat*`、`VACUUM`/rebuild 的 closed-world fail-closed 运维边界已记录。

## Timeout and Credential

- [x] deadline 同时覆盖 fetch 与 bounded body read。
- [x] presentation timeout 后 no native review/no mutation。
- [x] credential 使用 same-fd fstat 与 bounded read。
- [x] secret/error/output boundary 有测试。

## Backend Lifecycle and Transport

- [x] stop/restart/quit 使用 confirmed-termination barrier，`SIGTERM` 后按需 `SIGKILL`。
- [x] 无法确认旧 child 终止时保留 ownership 并拒绝 replacement spawn。
- [x] Flask debug 恒定 `use_reloader=false`，动态 process-tree focused oracle 只观察到一个 backend root。
- [x] health `Content-Length` 和 actual stream 均受 8,192-byte 上限。
- [x] strict HTTP JSON 在 decoder 前做 string-aware 32-level lexical-depth precheck。

## Release Gate

- [x] 当前 final Core local evidence：B1A 42/42、B1 相关 78/78、Blueprint API/contract/state-machine 28/28、plugin 228/228、Ruff/diff-check 通过。
- [x] 实际 local writer validation 使用 Python 3.14.2 + SQLite 3.53.4；旧 `.venv` Python 3.11.11 + SQLite 3.47.1 被 admission 退出 78。
- [x] 两次 `npm run check` 与 dependency audit 全通过；两轮均为 Core/unit 289、plugin 228、Node combined 86、renderer models 46，dependency audit 为 0 个已知漏洞。
- [x] 独立 Core 二次复审 `P0=0 / P1=0 / Go`。该 Go 仅准入当前 Core local implementation，不代表整仓发布。
- [ ] remote GitHub Actions CI 已在最终 diff 上运行；本地 CI-equivalent 不得替代此记录。
- [x] Spec/plan/tasks/evidence/version 只按真实完成状态更新；0.10.1 仍明确为 local candidate，未冒充已发布版本。
