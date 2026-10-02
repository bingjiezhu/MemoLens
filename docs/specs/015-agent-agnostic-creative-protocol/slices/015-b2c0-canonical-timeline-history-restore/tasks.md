# Tasks: ML-015-B2C0

- 实施状态：`IMPLEMENTED IN SHARED WORKTREE / PROMOTION BLOCKED`
- 说明：`[x]` 表示已有代码和本地证据；独立 promotion review 与最终整仓门禁已完成，真实 Codex/DeepSeek host journey 仍保持未勾选并阻断 promotion。

## Contract and migration oracles

- [x] T001 冻结 restore request/result、K/N/upstream/source preconditions 与 stable errors。
- [x] T002 实现 pure K→N+1 re-envelope/replay contract，只改 revision/parent。
- [x] T003 冻结 V13 action/event schema，保证 `timeline.apply_edit` 不隐式授予 restore。
- [x] T004 添加 populated V12→V13、backup、fault rollback、collision/future/tamper 谕言。
- [x] T005 保留 V12 rows/JSON/digests/event hashes 和 Core/plugin exact manifest parity。

## Core and APIs

- [x] T010 在 Timeline service 实现 desktop restore transaction。
- [x] T011 扩展 ledger cold audit，以 restore 独立分支重放 K→N+1。
- [x] T012 实现 desktop restore route 与 strict JSON/CAS/idempotency。
- [x] T013 实现 paired restore route，绑定 exact action/path/body/nonce/proof。
- [x] T014 验证 race、stale upstream/source、fault rollback、replay 和 cold reopen。

## Desktop workspace

- [x] T020 增加 Timeline historical selection model，不放宽 current-head validator。
- [x] T021 增加 revision K 只读检查和 `Stage restore revision K`。
- [x] T022 增加独立 pending restore 面板与 `Save revision K as N+1`。
- [x] T023 成功后 project refresh + exact Timeline reread；冲突/不确定不自动 rebase。

## Codex / DeepSeek Canonical Editor

- [x] T030 扩展 pairing action selection；edit-only 页面可只读检查历史，但不可 stage/save restore。
- [x] T031 在同一 canonical handoff 中分离 current snapshot 与 historical selection。
- [x] T032 Browser 只发 stage intent，exact K ref 由 server-held state 提供。
- [x] T033 实现 paired restore save/replay/reread validator 和 post-commit reconciliation。
- [x] T034 扩展 Codex/DeepSeek 共用 skill/prompt/README，不新建 host-specific restore 逻辑。

## Verification and promotion

- [x] T040 运行 Core/service/API/migration focused warnings-as-error matrix。
- [x] T041 运行 renderer/plugin/editor security/recovery/full plugin matrix。
- [ ] T042 执行 Codex→DeepSeek 与反向 fresh canonical restore journey。
- [x] T043 独立复审 authority、single source of truth、ledger replay 和 UI 诚实性；实现级 P0/P1 为 0，但 T042 仍阻断 promotion。
- [x] T044 运行 final repository gate，回填 exact counts/runtime/residuals。

## Deferred beyond B2C0

- [ ] 完整 cross-resource unified operation history。
- [ ] 通用 undo/redo/branch/merge。
- [ ] 跨 upstream restore 与 automatic replan。
- [ ] 新 edit dialect、音轨、字幕与 final-fidelity preview。
