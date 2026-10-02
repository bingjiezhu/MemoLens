# Implementation Plan: ML-015-B0

- 状态：`COMPLETE / VALIDATED`
- 原则：先冻结合同，再做 migration/Core，最后接只读 Agent surface；任何阶段都不扩大为用户确认或 Agent 写权限。

## Architecture

```text
desktop-authenticated HTTP
  → strict HTTP JSON gate
  → BlueprintService normalized command
  → MediaRepository BEGIN IMMEDIATE
       durable replay first
       exact project/head/base/reference proof
       compile + validate persisted Blueprint/v1
       append Blueprint-scoped operation
       append revision
       CAS explicit head
       freeze durable receipt

safe-default CLI/MCP
  → MemoLensGateway
  → one private read-only SQLite snapshot
  → exact head/revision/operation validation
```

## Phases

### Phase 1：合同与 schema

1. 冻结 B0 的 command/response/error、persisted document、authority与digest语义。
2. 新增 app-owned persisted Blueprint/v1 schema，并在 plugin包内保留 exact-byte copy；测试 digest parity。
3. 新增纯 schema/domain validator、semantic/unit digest与typed section diff compiler。

### Phase 2：V4 migration 与持久原语

1. 在任何 DDL 前增加 future-version preflight；保持 V2/V3 checksum不变。
2. v3→v4 前使用 SQLite Backup API生成本地受限权限 backup + SHA manifest；fresh DB不产生无意义备份。
3. 添加 V4 relations、indexes、immutable triggers和schema-version downgrade trigger；迁移单事务并做 integrity/foreign-key check。
4. repository 实现 exact head/revision/operation reader、durable receipt replay和单事务 commit/restore原语。

V4 使用 `creative_blueprint_operations`，不提前占用“统一 project ledger”命名。B2在Coverage/Timeline/UI进入同一链时再定义可扩展resource envelope、迁移投影与增量审计；B0读写为了发现任意祖先损坏，优先执行完整链验证。

### Phase 3：Core/API

1. 新增 `BlueprintService`，所有 actor/origin/effect/authority均由服务端常量生成。
2. POST body用 strict raw decoder；DB path仅作现有 transport binding，不进入 normalized command或持久记录。
3. replay在任何易变状态准备前执行；响应以 digest验证并通过 header表达 replay，不改写冻结 JSON。
4. 新增 current/exact/history GET；Blueprint 成为 canonical head 后，阻断 legacy Timeline 新建、revision 与新 render，把已有 Timeline 降为历史只读，并让 legacy Workbench 返回 canonical 指引。

### Phase 4：safe-default read surface

1. allowlist V4 relations，新增 persisted Blueprint reader/presenter。
2. 增加 CLI `blueprint-get/history` 与 MCP同名工具；不增加 write tool。
3. project-open优先有效 Blueprint head；head损坏不回退，legacy shadow语义不变。
4. status准确报告 Blueprint read/ledger 与 `write_blueprint=false`、`complete_project_history=false`。
5. legacy Video Workbench未支持Blueprint期间，项目GET在有效head存在时返回稳定capability error，不把legacy brief继续显示为current。

### Phase 5：验证与独立复审

1. migration、schema、authority、CAS/idempotency、fault injection、read parity专项测试。
2. plugin/backend/Node/renderer/typecheck/lint/compile/schema/diff全量回归。
3. 独立产品/架构与安全审查；无 P0/P1/P2后才标记 `IMPLEMENTED / VALIDATED`。

## Rollout / Rollback

- V4 是 additive；不自动创建任何 Blueprint。
- 当前实现没有运行时 Blueprint POST kill switch；停止新写需要发布显式拒绝 commit/restore POST 的 forward fix 或维护版本。已有 revision/head/history 继续可读，这不是配置级即时开关。
- 不执行 destructive down-migration；migration失败使用生成的 v3 backup人工恢复，首个 Blueprint mutation后只允许 forward-fix或只读导出。
- 旧 v3 binary正常启动将被 downgrade trigger拒绝，避免 schema标记回退。
