# Requirements Checklist: ML-015-B2B

- [x] Blueprint、Coverage Plan、Timeline execution 和 approval/export 的真源/权限边界分离。
- [x] 前置固定为 exact current Blueprint + exact current A1 baseline Coverage，不接受客户端自报文档。
- [x] 只接受能从 exact Blueprint 重放的 A1 baseline Coverage，且 zero-gap、每 Beat 恰好一个 selected assignment；Gap 不隐式填充，manual/global Coverage 尚未扩展。
- [x] Lowerer 只做 1:1 hard-cut，明确禁止重规划、转场、字幕、任何音轨编排和速度变化；v1 只有 source audio disabled 负能力。
- [x] Fixed source binding 的确定性解析、无路径 manifest、freshness 与 no-auto-failover 已定义。
- [x] Timeline document 与 execution-local source manifest 分存；document 显式包含 revision-1/parent/upstream operation binding，但不含 own operation/time/path/opaque source ID。
- [x] V7 additive operation/revision/head/receipt schema、empty-head→revision-1 CAS、idempotency、fault/tamper/rollback 边界已定义；successor/history 未被写成已实现。
- [x] Existing-head-wins 与无自动 materialize/无 conflict 自动写重试已定义；不伪造 manual/restore origin 分类。
- [x] legacy Timeline 只作 `historical_observed_context_only`，不迁移或升级为 canonical current。
- [x] Draft、approval not established、not exportable 与任何下游 preview/export 权限已分离。
- [x] UI 只提供 explicit materialize/refresh 和 inspect；B2B app preview 明确 deferred，ML-017-A export 是独立 authority。
- [x] Current/exact-revision GET、Desktop POST、MCP capability、database/empty-head precondition 和 strict adoption 边界已定义；不声称已有 history-list GET。
- [x] 每个用户故事都有 independent test 设计，成功准则包含 determinism、initial-head concurrency、tamper、preview 和 legacy regression；尚未执行的全矩阵不由本清单勾选推导为已通过。
- [x] V1–V6 checksum/schema 保持、current-schema fresh DB 中 exact V7 catalogue、V6→current 经 V7/V8、collision/recovery 和 final-diff promotion gate 已定义。
- [x] 手工 edit/restore、unified history、global planner、craft、approval/export/usage 已明确 deferred，不在 B2B 内偷渡。

> 本清单勾选只表示规格覆盖已审阅，不表示代码已验证。实施进度只以 [tasks.md](../tasks.md) 和 [implementation-evidence.md](../implementation-evidence.md) 为准。
