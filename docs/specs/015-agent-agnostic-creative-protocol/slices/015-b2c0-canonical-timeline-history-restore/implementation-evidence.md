# Implementation Evidence: ML-015-B2C0

- 证据快照：2026-08-23，本地 shared dirty worktree
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCAL REPOSITORY GATES PASSED; PROMOTION BLOCKED`
- 发布状态：`NOT COMMITTED / NOT TAGGED / NOT RELEASED`
- Remote CI：`NOT RUN`

## Implemented path

- Core V13 `paired_agent_canonical_timeline_restore` 以显式 migration 扩展 capability/event closed union；V12 migration/checksum 和历史 receipt/event/digest 不改写。
- `timeline.restore_revision/v1` 只接受 exact current N 与 historical K identity。事务内重读 project、N、K、Blueprint、Coverage 与 fixed sources，然后以 K payload 仅重包 `revision=N+1`、`parent=N`，追加 operation/revision/head/generic v2 receipt/use event。
- Desktop 与 paired routes 共用同一 `TimelineLoweringService.restore_revision` 和 Core contract；paired route 另行绑定 `timeline.restore_revision`、path、body、single-use nonce、HMAC proof 与 permanent replay identity。
- React workspace 把 current N、read-only K、pending edit 与 pending restore 分开。任意旧 K 可检查；只有 exact same-binding K 才可 stage restore。Export 始终只读 current canonical head。
- Codex 与 DeepSeek Harness 共用同一 `memolens_canonical_editor_handoff(project_id)`、Python editor server 和 HTML 页面。Browser 只提交 revision number 与 stage intent；完整 K、source facts、credential、nonce 和 proof 留在插件进程。
- Canonical Editor 显示 move、trim、image duration、same-Beat replacement，以及历史 K、`Stage restore revision K`、`Save K as N+1`。只读 history 不授予 restore authority；edit 与 restore pending 互斥且都标记 noncanonical。
- Post-commit/reread 不确定保留原 pending 与幂等 identity；只有 refreshable head/binding/source conflict 才刷新 current 并丢弃旧 pending，永不自动 rebase。

## Focused evidence observed

- Restore pure contract/service/Desktop API：**7/7 passed** with `PYTHONWARNINGS=error::ResourceWarning`。
- Renderer edit/preview/restore models：**16/16 passed**。
- Canonical Editor handoff/security/recovery/history/restore：**16/16 passed** with `PYTHONWARNINGS=error::ResourceWarning`。
- Electron authority presentation/action-bound contract：**25/25 passed** after fresh Electron build。
- TypeScript renderer + Electron typecheck：passed。
- 完整 plugin discovery：**293/293 passed in 62.869 s**；Renderer models：**99/99 passed**。
- `plugin-creator` manifest validator 对 source 与 installed cache 均通过。Codex 已安装并启用 `0.10.1+codex.20260824043745`，source↔cache 无差异，cache 包含 history/restore UI、server、skill 与 MCP metadata。
- 最终 `npm run check`：**exit 0**，Python **563/563**、plugin **293/293**、Node/Electron **108/108**、renderer **99/99**，并通过 local deploy verify、typecheck/build 与 `git diff --check`。Aggregate Python 仍会输出既有 SQLite finalizer `ResourceWarning`，不声称 warning-clean。
- 本地 in-app Browser 视觉验收：实际页面显示 current `N = 2`、read-only `K = 1`、visible trim/replacement controls、`Stage restore revision 1` 和 `Save K1 as N3`；stage 后明确显示 `Pending · not canonical`。console error/warning 为 0。该证据使用真实 editor server/UI 与受控 fixture backend，不冒充真实 Codex/DeepSeek model journey。

## Promotion blockers and non-claims

- Final repository gate、plugin validation/reinstall/cache parity 和独立复审已闭合。
- T042 真实 Codex→DeepSeek 与 DeepSeek→Codex model/host/UI journey 未运行；自动分离 adapter-process harness 不能替代真实 host。
- T043 独立 promotion review 已完成：实现级 P0/P1 为 0；T042 仍使 promotion 保持 NO-GO。
- 本切片只交付 Timeline-local history/restore foundation；不包含跨 Blueprint/Coverage/Timeline/Export/Usage 的 unified history、通用 undo/redo/branch/merge 或跨 upstream restore。
- 未运行 Remote CI，未 commit/tag/release。
