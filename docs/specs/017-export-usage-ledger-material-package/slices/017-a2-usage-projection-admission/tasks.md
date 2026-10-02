# Tasks: ML-017-A2

- 状态：`IMPLEMENTED IN SHARED WORKTREE / LOCAL VALIDATION WITH RESIDUALS`

## Projection and integrity

- [x] 实现 half-open union/residual、image occurrence semantics 和 750-fixture oracle。
- [x] 实现五表 UNION cold audit，关闭 occurrence 与 operation+occurrence 删除 fail-open。
- [x] 在 analysis writer 和 projection 两层验证 source duration/candidate domain。
- [x] 接入 search policy、ranking、presentation、Usage revision 和 UI explanation。

## Authority and UX

- [x] 实现 Renderer closed 11-field strict adoption 与 whole-response failure。
- [x] 实现 9+11 `usage_selection`、policy/search selection reset 和 partial-used no-reuse guard。
- [x] 实现 Director same-transaction revalidation、409 conflicts、provenance digest 和 zero partial writes。
- [x] 实现 bounded export job polling/unknown recovery/manual Refresh fallback。

## Verification

- [x] Export+Usage+admission strict lane：92/92。
- [x] video API 12/12；renderer models 84/84；typecheck/renderer build passed。
- [x] backend admission 7/7，含 route 400/409 oracle。
- [x] 既有 real Electron native export 已保存五角色 package；自动 polling 改动后的 fresh Electron 尚未重跑。
- [x] 在最终 diff 上运行完整 `npm run check`：Core 506、plugin 231、Node/Electron 106、renderer models 84，exit 0。

## Deferred

- [ ] Exact residual clip identity/selection。
- [ ] Derivative-output exclusion 和 Usage correction/supersession。
- [ ] ML-017-B full package、Remote CI、release。
