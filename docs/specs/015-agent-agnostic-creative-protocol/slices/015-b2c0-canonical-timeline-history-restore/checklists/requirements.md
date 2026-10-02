# Requirements Checklist: ML-015-B2C0

- [x] 明确只是 Timeline-local history/restore foundation，不冒充完整 B2C。
- [x] current head authority 与 historical selection 状态分离。
- [x] append-only restore、parent=N 与 restore_from=K 语义明确。
- [x] 解释 revision/parent 导致 N+1 digest 不会与 K 整份 digest 相同。
- [x] K/N/Blueprint/Coverage/source/CAS 信任边界明确。
- [x] `timeline.restore_revision` 为独立 action，不由 edit capability 继承。
- [x] V13 不修改 V12 checksum 与历史 digest。
- [x] Codex/DeepSeek/Desktop 共用 canonical head，不创建 host-local history。
- [x] concurrency、idempotency、tamper、migration、crash/recovery 可证伪。
- [x] undo/redo/branch、跨 upstream restore 与 final-fidelity 能力明确 deferred。
