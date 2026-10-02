# Tasks: ML-018-A1 Canonical Coverage Plan Baseline

## Phase 1 — Contract

- [x] T001 冻结 Coverage Plan v1 closed schema、limits、canonical digest 与 deterministic IDs。
- [x] T002 实现 script blocks → Beat/Need/estimated slot 的纯编译器。
- [x] T003 实现 material hints → selected/alternative/gap 的确定性 baseline 规则。
- [x] T004 添加 malformed、duplicate、unresolved、short-span、path-leak 与 replay contract tests。

## Phase 2 — Persistence

- [x] T010 添加 V6 additive migration、preflight collision、physical schema verification 与 v5 recovery backup。
- [x] T011 添加 immutable operation/revision/head/receipt repository API 与 CAS。
- [x] T012 抽取无路径 evidence proof resolver；保持 Blueprint manifest 向后兼容。
- [x] T013 添加 fresh V6、v5→v6、tamper、rollback、foreign-key 与 integrity tests。

## Phase 3 — Service and API

- [x] T020 实现 Desktop-authenticated `coverage.materialize_baseline` command 与 idempotency。
- [x] T021 实现 current/exact plan read、freshness 与 bounded history projection。
- [x] T022 添加 API strict JSON、database identity、stale Blueprint、CAS、replay/conflict tests。
- [x] T023 把 current Coverage state 接入 canonical project workspace，不虚报 Timeline 可执行。
- [x] T024 添加 renderer closed types、strict normalizer 与 exact Blueprint/Coverage adoption guard。
- [x] T025 接入 path-free Coverage resource、Desktop materialize/refresh 和 stale/conflict recovery。
- [x] T026 显示 Beat、selected assignment、alternative 与 honest gap，并保持 Timeline/preview 不可用。

## Phase 4 — Verification

- [x] T030 在最终 diff 上运行 40 项 focused Core/persistence/API tests。
- [x] T031 在最终 diff 上运行 100 轮并发 CAS 与故障注入 tests，证明无 silent overwrite/partial publication。
- [x] T032 运行整仓 gate，记录 runtime identity、base commit、exact counts 与残项。
- [x] T033 复核 spec/plan/tasks/code 一致性并更新实现证据。
