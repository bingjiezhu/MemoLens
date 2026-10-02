# Implementation Plan: ML-018-A1 Canonical Coverage Plan Baseline

## Architecture decision

Coverage Plan 是 Blueprint 与 Timeline 之间唯一的素材规划真源。A1 只做 deterministic baseline materialization：读取 exact canonical Blueprint，在同一事务中解析 evidence，生成不可变 plan revision 并 CAS 更新 head。它不做新的语义检索，也不把 Timeline 当计划存储。

```text
Blueprint head + expected Coverage head
  → validate exact Blueprint ledger
  → derive Beat/Need from script blocks
  → resolve material_hints against canonical evidence
  → deterministic assignment + honest gaps
  → validate canonical Coverage document
  → append operation + revision + CAS head + receipt
```

## Data contract

Canonical document v1 顶层只包含：

- identity：object/schema/project/revision/parent；
- exact `blueprint_binding`；
- `compiler`（稳定 ID、规则版本、能力声明）；
- `output_binding`（target duration/aspect ratio）；
- ordered `beats`；
- sorted `evidence_manifest`。

领域文档不保存 operation lineage 或 wall-clock time。operation/receipt envelope 在持久层固定 actor、request/result 与时间元数据，避免把提交历史复制进 Coverage 内容真源。

Beat 只保存 `script_block_id` 与 `text_sha256`，不复制第二份可编辑 script。每个 Beat 包含 estimated slot、一个 baseline Need、selected assignments、alternatives 与 Gap。

## Persistence

新增 additive schema migration，不改写用户媒体：

- `coverage_plan_operations`
- `coverage_plan_revisions`
- `coverage_plan_heads`
- `coverage_plan_receipts`
- 必要 index 与 immutable triggers

所有 JSON 使用 canonical encoding 与 digest。新 migration 前创建受限、校验过的 schema backup；启动时验证物理 schema、migration checksum、foreign keys 与 integrity。

## Components

- `core/coverage_contract.py`：纯 contract、deterministic compiler 与 validator。
- `core/media_db.py`：V6 migration、evidence proof、operation/revision/head repository 方法。
- `backend/src/media/coverage.py`：命令服务、CAS、幂等、freshness/read projection。
- `backend/src/api/routes.py`：Desktop-authenticated materialize 与 read endpoints。
- `backend/src/__init__.py`：service wiring。
- `src/blueprint/coverageTypes.ts`：closed renderer contract。
- `src/blueprint/coverageModel.ts`：strict normalization、identity/freshness adoption guard。
- `src/blueprint/coverageApi.ts`：path-free GET 与 Desktop-authenticated POST client。
- `src/blueprint/BlueprintProjectWorkspace.tsx`：materialize/refresh、Beat/assignment/alternative/gap inspection 与 honest Timeline-unavailable state。
- `tests/`：contract、migration、state machine、API 与 tamper/fault tests。

## Safety and failure model

- Agent/UI 只能传 expected heads；proof 由 Core 解析。
- unresolved evidence 不可 selected。
- 事务提交前完成 document、digest 与 referential validation。
- unknown/tampered persisted JSON fail closed；不回退到 legacy brief。
- A1 不改原件、不调用模型、不联网、不渲染。
- path-free 保证只覆盖 Coverage canonical document、API response 与 renderer state；本地 HTTP transport 仍沿用数据库定位参数，不能扩大成日志/URL 全局无路径承诺。
- append-only ledger、digest、trigger 与 trusted-read floor 能发现受保护历史的部分回退/篡改；在没有外部 sealed checkpoint 时，攻击者离线替换整份自洽数据库仍不在密码学防回滚承诺内。

## Promotion gate

先运行 focused tests，再运行整仓 `npm run check`。只有 migration、determinism、CAS、tamper、workspace freshness 和 resource lifecycle 都通过，才能标记实现完成。
