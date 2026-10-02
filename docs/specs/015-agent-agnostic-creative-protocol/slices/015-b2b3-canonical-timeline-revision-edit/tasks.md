# Tasks: ML-015-B2B3

## Contract

- [x] T001 冻结四种 closed edit shape、limits 与稳定 errors。
- [x] T002 实现 pure apply/reflow/parent binding 与 deterministic replay verifier。
- [x] T003 为 trim/duration/move/replace 添加正反、tamper、duplicate evidence与limits tests。

## Ledger and service

- [x] T010 添加 V10 metadata-only migration与V9 populated preservation oracle。
- [x] T011 扩展 command/receipt/operation parser，result持久保存 exact edit。
- [x] T012 让 cold audit按 command type选择 Coverage derivation或parent edit replay。
- [x] T013 实现 transactional apply-edit service、replacement resolver、CAS/idempotency/fault tests。
- [x] T014 新增 Desktop-authenticated closed POST API与稳定4xx tests。

## Renderer and workbench

- [x] T020 添加 edit types、command normalizer/API/idempotency key。
- [x] T021 添加 pending edit model；只从 exact current head创建，保存后canonical reread。
- [x] T022 在 canonical clip controls 接入 trim、image duration、move和same-Beat replace。
- [x] T023 添加 Discard / Save revision N+1 / stale conflict UX与键盘可用性测试。
- [x] T024 验证 inspection pending preview不产生render/export/Usage。

## Verification

- [x] T030 运行 focused warning-as-error Python、Renderer model、typecheck/build。
- [x] T031 独立审计 authority、ledger replay、source binding和UI state边界。
- [x] T032 运行 final repository gate，记录exact counts、runtime与residuals。
- [x] T033 完成 fresh real-SQLite/API edit→save→reopen→inspection journey并保存证据（人工 Codex Browser journey 保留为 residual）。
- [x] T034 回填 implementation evidence、README、Spec index和roadmap。

## Deferred

- [x] 后续 B2B4/B2C0 已在 shared worktree 实现 Codex/DeepSeek canonical handoff、paired write、V12 receipt convergence 与 V13 history/restore；fresh 双向真实 host/model/UI journey 仍由后续切片追踪。
- [ ] B2C undo/redo/restore/branch/unified history。
- [ ] 字幕、任何音轨、transition、final-fidelity preview和multi-track。
