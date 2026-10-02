# Implementation Plan: ML-015-B2C0

## Scope

在已完成的 V12 receipt convergence 上，为 canonical Timeline 增加只读历史选择和 append-only restore foundation。本切片已在 shared worktree 实现，但不实现完整 B2C unified history。

## Architecture

1. `core/timeline_restore_contract.py` 冻结 K→N+1 exact re-envelope 和 replay validator。
2. `core/media_db.py` 以 V13 显式加入 `timeline.restore_revision` action/event admission，保留 V12 generic receipt authority。
3. `TimelineLoweringService` 在 verified transaction 内重读 N/K/upstream/source，追加 restore operation/revision/head/receipt。
4. Desktop 与 paired routes 分别维持原有 authentication/provenance，共用 Core restore contract。
5. React 和 Canonical Editor 把 current workspace 与 historical selection 分开；显式 Save 后重读 canonical head。

## Integrity model

- K 和 N 必须在同一 cold-audited Timeline ledger。
- restore operation 以 exact K 和 N 为输入，不接受 caller-supplied result Timeline。
- K 的 payload 只能改 revision/parent envelope；source bindings 字节不改。
- head CAS、operation/revision/receipt/event 在同一事务成功或全部回滚。
- historical selection/pending preview 不进入 Export 或 Usage。

## Schema sequence

- V12 checksum 与物理 schema 不改。
- V13 重建 capability/event CHECK 约束以加入 restore action，保留所有旧 row/digest/hash。
- Core 与 standalone plugin 必须冻结同一 V13 manifest/checksum/future refusal。

## Verification lanes

- pure restore contract
- V12→V13 migration and historical preservation
- desktop/paired service/API/proof/CAS/fault/cold audit
- renderer history selection + pending restore state
- canonical editor security/recovery + Codex/DeepSeek parity
- focused warnings-as-error and final repository gate

## Rollback

V13 migration 失败时保留 verified V12 backup 与 exact V12 database。功能回滚时停止发出 restore command，不删除已追加的 restore revision。
