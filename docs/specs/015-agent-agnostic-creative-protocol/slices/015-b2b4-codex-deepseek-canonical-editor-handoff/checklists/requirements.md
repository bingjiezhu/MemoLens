# Requirements Checklist: ML-015-B2B4

- [x] 实施状态明确为 `IMPLEMENTED IN SHARED WORKTREE / PROMOTION BLOCKED`，已实现、聚焦验证和未验证边界分开。
- [x] Codex 与 DeepSeek Harness 共享同一 `database_uuid/project_id/canonical head`，不依赖聊天历史或 host-local Timeline。
- [x] project-bound canonical handoff 只接受 `project_id`，不接受 caller Timeline、source、candidate 或 path。
- [x] canonical editor 只允许 B2B3 的四种 closed edit，没有偷渡 legacy 方言。
- [x] paired `timeline.apply_edit` 的 effect class、scope、nonce/HMAC、CAS、idempotency 和 server-derived actor/origin 已明确。
- [x] permanent paired provenance 精确链接 capability use、receipt、Timeline operation/revision/head，不伪造 desktop receipt。
- [x] V11 过渡、已完成 V12 convergence、V13 restore、历史保存、Core/plugin schema parity、backup/rollback 和 closed-world audit 已明确。
- [x] T011 已修订为 V12 versioned generic receipt：legacy v1 只表达 request digest，Timeline v2 才表达 exact request/actor/origin；不允许伪造历史正文或保留第二 authority table。
- [x] Browser 不取得 pairing/main/desktop secret，只通过插件进程内 credential client 保存。
- [x] one-time URL、session、Host/Origin/CSP/body/TTL/count 和 secret/path redaction 有可证伪要求。
- [x] Save 后 canonical reread 而非 optimistic local N+1；conflict/stale 不自动 rebase。
- [x] capability check/use、Timeline mutation、receipt 和失败回滚的单事务边界已明确。
- [x] response loss、过期、撤销、耗尽、restart、并发 CAS 和 stale source 的恢复语义已明确。
- [x] legacy editor 明确保留为 **Unsaved Draft Lab / Not saved / process-scoped**，不能进入 canonical write。
- [x] Electron 只保留 pairing approval/revoke 和 runtime trust，不是 B2B4 主编辑界面。
- [x] Codex/DeepSeek 双向 N→N+1→N+2 fresh journey、安全负向矩阵、迁移矩阵和整仓 gate 均是推广前置。
- [x] B2C、新 edit dialect、音频/字幕/final-fidelity、remote editor 和 release 均明确不在本切片。
