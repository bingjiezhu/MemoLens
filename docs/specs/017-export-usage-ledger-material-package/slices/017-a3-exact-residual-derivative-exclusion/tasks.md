# Tasks: ML-017-A3

- 实施状态：`IMPLEMENTED / VALIDATED_CONTROLLED_LOCAL`
- 验证状态：`FOCUSED + FIRST FULL LOCAL GATE PASSED / PROMOTION BLOCKERS RETAINED`
- 说明：已勾选项均有当前 dirty shared worktree 证据；不把 loader/cache/UI fixture 写成真实 model call、Remote CI 或 release。

## Contract and adversarial oracles

- [x] T001 冻结 `rseg_<64hex>` canonical preimage、closed `residual_binding`、stable errors与资源上限。
- [x] T002 冻结五表 cold-audited successful Export output SHA fact set和 derivative revision。
- [x] T003 冻结 executable-material no-bypass、reference-only可见与 exact-SHA rename/copy语义。
- [x] T004 建立 interval、collision、parent/source/analysis/usage/derivative substitution和 tamper失败 oracle。

## Backend derivative exclusion

- [x] T010 实现 transaction-local五表 census/cold audit和 validated successful `video_sha256` projector。
- [x] T011 在 photo/video ordinary/residual candidate形成前按 full asset SHA过滤。
- [x] T012 把 derivative fact revision纳入 search revision与same-transaction admission。
- [x] T013 验证 failed/cancelled/interrupted不误排，tamper/validator-unavailable不失败开放。

## Plugin parity

- [x] T020 在 `ReadOnlyMemoLensStore.mixed_material_search` 的一个 private snapshot内实现完整投影/filter/rank/present。
- [x] T021 实现 plugin-local current Export validator parity并禁止 legacy fallback。
- [x] T022 验证 backend、source plugin、installed Codex cache和 DeepSeek bundle的候选/错误完全一致。

## Residual search and UI

- [x] T030 实现 deterministic residual ID/binding parser、validator和collision refusal。
- [x] T031 从 current parent Usage projection生成 exact residual candidates，不新增 segment row。
- [x] T032 修复 presenter/thumbnail/detail为 parent-resolved route，不 direct lookup `rseg_*`。
- [x] T033 更新UI/types以显示 parent、exact range、selection和“successful output bytes” exact-SHA derivative exclusion解释；不声称删除原件。

## Brief admission

- [x] T040 扩展 `usage_selection`与Brief provenance以冻结完整 residual/derivative binding。
- [x] T041 Director在同一 immediate transaction内重算 parent、source、analysis、Usage、residual和derivative facts。
- [x] T042 验证 concurrent Export/analysis/source/same-SHA import产生409且project/Brief/idempotency零部分写。
- [x] T043 验证same-request replay精确，different-binding rebind冲突。

## Timeline and canonical chain

- [x] T050 legacy Timeline使用exact parent/source resolver与residual interval，移除preferred-source fallback。
- [x] T051 Blueprint新增closed `residual_span` executable proof union，reference-only保持独立。
- [x] T052 Coverage保留完整binding；Timeline使用parent `seg_*` + exact interval + residual proof。
- [x] T053 Export actual-read/Usage/cold replay验证residual lineage和历史不可变。

## Verification and promotion

- [x] T060 运行controlled-local production-code partial-used→residual→Brief→legacy/canonical Timeline→Export纵向旅程。
- [x] T061 运行controlled-local production-code successful export→same-SHA rename/copy reimport→next-search排除旅程。
- [x] T062 运行production inventory/oracle、focused warning-as-error、full backend/plugin/Node/renderer/typecheck/build。
- [x] T063 fresh cachebuster安装Codex plugin并验证source↔cache parity；official DeepSeek fresh-profile loader parity。
- [x] T064 第一轮 full `npm run check`、A3 UI current renderer suite、whitespace check和独立P0/P1审查已通过；所有后续代码合并后的 final current-diff 整仓重跑仍由根任务回填。
- [x] T065 仅按observed结果回填implementation evidence；Remote CI/release与真实host/model/UI未运行时明确保留。

## Deferred beyond A3

- [ ] perceptual/near-duplicate、转码后视觉相似和模型推断的 derivative识别。
- [ ] Usage correction/supersession与final/test角色修正。
- [ ] 非线性source mapping、完整素材包、relink、发布、上传和远端release。
