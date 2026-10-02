# Tasks: ML-015-B2B2

- 状态：`IMPLEMENTED IN SHARED WORKTREE / LOCAL VALIDATION WITH RESIDUALS`

## Persistence and command

- [x] V9 migration、recoverable backup、same-name operation/receipt rebuild 与 physical schema oracle。
- [x] 保留 populated V8 Timeline 与 Export/Usage rows、digests 和 foreign keys。
- [x] 实现 reconcile closed request、Desktop authority、expected head、parent、`N+1`、CAS 和 permanent receipt。
- [x] 扩展 cold audit 到完整 multi-revision history。

## Service and renderer

- [x] 接入 Backend service/API/workspace capability 与 stable conflicts。
- [x] 接入 Renderer types/model/API、stale reason parity、explicit reconcile/refresh。
- [x] 实现只读 hard-cut inspection preview、same-origin media、identity reset 和 fail-closed errors。
- [x] 将 preview model tests 纳入默认 renderer-model gate。

## Verification

- [x] Coverage+Timeline strict focused lane：106/106。
- [x] populated V8→V9 artifact：Timeline 1/1/1/1；Export 2 operations/2 jobs/2 revisions/4 Usage/2 receipts；foreign-key violations 0。
- [x] preview model 5/5；renderer models 84/84；typecheck/build passed。
- [x] 在最终 diff 上重跑完整 `npm run check`：Core 506、plugin 231、Node/Electron 106、renderer models 84，exit 0。
- [ ] 真实 Electron reconcile + inspection 的截图/交互证据。

## Deferred

- [ ] Canonical manual edit、restore/branch/undo/redo 与 manual-current rebase。
- [ ] Final-fidelity preview、字幕、转场与任何音轨能力。
- [ ] Remote CI、clean-machine、tag/release。
