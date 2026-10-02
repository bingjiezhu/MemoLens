# Implementation Plan: ML-015-B2B3

## Scope

在不建立第二份 Timeline 真源的前提下，为 canonical Timeline 增加首批可重放手工 edit、append-only revision 和 React 工作台控制。

## Architecture

1. `core/timeline_edit_contract.py` 负责 closed edit normalization、pure application 和 exact replay verification。
2. `core/media_db.py` 将 schema 升为 V10，只扩展 command admission/migration history；revision/head physical schema不改。
3. `TimelineLoweringService` 增加 `apply_edit`，在 verified transaction 中解析 replacement source、执行 pure contract、append revision/operation/head/receipt。
4. `POST /v1/creative/projects/{id}/timeline/edit` 只接受 Desktop token、exact DB/head/upstream bindings和一个 edit。
5. React normalizer/API 将 command result与 canonical reread分开；工作台只在 current head上编辑，并在保存后重新读取。

## Integrity model

- materialize/reconcile revision 继续用 exact Coverage derivation验证。
- edit revision 必须以 operation result 内的 closed edit从 parent重新计算；Timeline JSON、source bindings、operation、receipt 任一不一致均失败。
- replacement source只由 repository transaction解析；客户端 assignment ID 不是 source authority。
- preview state不是 canonical state，刷新或失败必须丢弃。

## Rollback

V10 没有 destructive data rewrite。失败 migration回滚 metadata transaction；旧 V9 数据库先保留可恢复 backup。功能回滚时可以停止发出 `timeline.apply_edit`，历史 revision继续由 V10 reader审计，不删除。

## Verification lanes

- pure Core contract + deterministic replay
- V9→V10 populated migration and tamper
- persistence/service/API concurrency, stale preconditions and fault rollback
- Renderer model/API/state tests + typecheck/build
- focused warning-as-error Python lane
- final repository gate and fresh interactive journey
