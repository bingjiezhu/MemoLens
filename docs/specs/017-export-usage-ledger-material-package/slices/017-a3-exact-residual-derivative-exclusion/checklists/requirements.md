# Requirements Checklist: ML-017-A3

- 实施状态：`IMPLEMENTED / VALIDATED_CONTROLLED_LOCAL`
- 验证状态：`FOCUSED + FIRST FULL LOCAL GATE PASSED / PROMOTION BLOCKED`

## Identity and intervals

- [x] `rseg_*`只由closed canonical facts派生，跨host/Agent稳定并对任一binding变化敏感。
- [x] residual interval为整数half-open，和used union不重叠且精确覆盖candidate domain。
- [x] residual match携完整closed binding；synthetic identity不写入analyzed segment table。
- [x] presenter/thumbnail/detail通过exact parent resolver，不direct lookup synthetic ID。

## Authority and admission

- [x] Search只产candidate；Director在project/Brief/idempotency同一transaction内重算全部事实。
- [x] stale usage/analysis/source/derivative revision返回stable conflict并零partial write。
- [x] legacy Timeline禁止preferred-source fallback或扩大到parent full interval。
- [x] Blueprint→Coverage→Timeline端到端保留`residual_span` proof并可cold replay。

## Derivative exclusion

- [x] 五表project census与cold audit在任何absence claim前完成。
- [x] 只有successful且完整validated canonical output SHA贡献derivative fact。
- [x] failed/cancelled/interrupted不误排；tamper/validator-unavailable fail closed。
- [x] exact full asset SHA排除rename/copy/path变化，无filename/path heuristic。
- [x] 所有executable raw-material policy无derivative bypass；reference-only history仍可见但不可执行。
- [x] derivative revision进入search/admission identity，并发export或same-SHA import使旧selection stale。

## Backend and plugin parity

- [x] Backend在一个transaction snapshot完成candidate、usage、derivative、filter/rank/present。
- [x] Standalone plugin在一个private SQLite snapshot独立完成同义逻辑，不依赖backend service。
- [x] plugin-local validator与Core current schema/receipt/output proof parity，无法验证时不legacy fallback。
- [x] Codex和DeepSeek复用同一plugin code与shared UI，不复制business authority。

## Evidence and promotion

- [x] residual与derivative adversarial focused suites在warning-as-error下通过。
- [x] 两条controlled-local production-code vertical（residual消费、same-SHA成片排除）通过且记录。
- [x] production inventory/oracle、plugin validator、fresh Codex install/cache parity与DeepSeek fresh loader通过。
- [x] 第一轮 full repository gate、A3 UI current renderer suite、whitespace check和独立P0/P1 review通过；final current-diff 整仓重跑待根任务回填。
- [x] 未运行Remote CI/release、Codex/DeepSeek 真实 model call 与双向 host-model-UI T053/T054 已明确记录，不由fixture、loader或cache代替。
