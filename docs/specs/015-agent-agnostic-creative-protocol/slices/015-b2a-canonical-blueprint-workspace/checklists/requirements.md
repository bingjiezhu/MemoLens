# Requirements Quality Checklist: ML-015-B2A

- Spec 审查状态：`FROZEN / IMPLEMENTATION RECORDED`
- 实施验收状态：`LOCAL VALIDATION WITH RESIDUALS`
- 勾选语义：前四节检查规范是否已冻结要求；“Implementation Gates”只能在实际实现/验证后勾选。

## Product Alignment

- [x] 已将 Blueprint 项目无法打开的已证实 409 断点定义为本切片唯一主问题。
- [x] 已保留“对话不是真源，Blueprint/Timeline/history 才可恢复”的决策。
- [x] 已保留“可以一键先出一版，也可任意介入”的产品方向，但没有在 compiler 尚未实现时伪称已有 first cut。
- [x] 已把用户作为创意/立场决策者的 authority 与 Agent proposal 和可执行剪辑分开。
- [x] 已提供 Google Docs/Slides 式可见 revision/restore 入口，且 restore 不删除历史。
- [x] 已保留对话 + 可视化工作台共享 Core 状态的方向，不创建 renderer/localStorage 第二真源。
- [x] 已明确这个过渡切片是可用性跃迁的基础，不是用炫酷特效替代主链。

## Truth and Authority Precision

- [x] semantic truth 只来自 explicit validated Blueprint head，legacy brief/chat/cache 不可回退。
- [x] exact legacy-only 已限定为零 Blueprint/authority/capability trace；受支持 pairing/capability trace 证明曾有 canonical head，缺 head 时必须 fail closed。
- [x] technical canonical authority、Blueprint creation authority、user decision authority 三者已分开。
- [x] decision authority 绑定 exact `as_of_revision` 与 8 个 decision units，部分确认不伪装全部确认。
- [x] 8/8 confirmed 不自动推导 Timeline、render、export 或 publish authority。
- [x] executable truth 在 B2A 冻结为 `not_compiled/current_timeline=null`，legacy Timeline 只作历史。
- [x] restore 复制 semantic 而不复制历史用户 authority，新 revision 由 Core 重新投影。
- [x] authority confirm/revoke 仍由 Electron main 拥有的原生审阅路径执行，renderer 不获得新能力。

## History and Restore Precision

- [x] 3 条 lane 已冻结为 `semantic_changes`、`decision_authority`、`legacy_artifacts`。
- [x] 顶层已强制 `complete_project_history=false`、`global_total_order=false`、`replay_scope=per_lane_only`。
- [x] 每条 lane 的内容、可声称范围、不可声称、排序与截断语义已明确。
- [x] authority lane 已精确限定为 `event_id/sequence/authority_operation/observed_revision/decision_units/created_at`，nonce/token/receipt/path/runtime secret/raw presentation 全部禁止。
- [x] legacy lane/brief role 为 `migration_context_only`、Timeline role 为 `historical_observed_context_only`；精确 order literal、brief/Timeline 共享预算和 decode 前 1 MiB/16 MiB byte preflight 已冻结。
- [x] B2A history 已冻结为 latest bounded windows + available totals/truncation；无 cursor/has-more continuation，完整 unified history 明确留给 B2C。
- [x] history parent→result diff 与 current→selected compare 已区分。
- [x] 12 个 semantic section 的稳定顺序与 P0 diff 粒度已冻结。
- [x] restore 使用 exact expected head + exact target digest + durable idempotency receipt，不存在 UI-only write path。
- [x] CAS conflict 后 0 次自动写重试，success 后必须 Core refetch，不手工拼状态。

## Scope, Safety, and Failure Closure

- [x] 已冻结“无 schema/migration”，V1–V5 checksums 和 B1A exact V5 allowlist 不变。
- [x] 已冻结 canonical semantic/authority 损坏整体 fail closed，不回退 legacy/cached state。
- [x] 已冻结仅 legacy metadata lane 损坏时的局部诚实降级，不影响 canonical 真源身份。
- [x] 已对 stale latest-window sequence、runtime/database switch、stale child response、CAS race、target corruption、compiler absent 定义失败关闭结果。
- [x] 已限定 history 单 lane 最多 100 条，不接受 renderer 任意 SQL/order expression。
- [x] 已保持 MCP `write=false` 和 B1 paired proposal write 原边界，没有新 Agent authority/Timeline/file 能力。
- [x] 已排除 compiler、Global Assignment、unified ledger、render/export/usage/package 和跨域 undo/redo/branch。
- [x] 已排除模型、网络、shell/subprocess/FFmpeg、媒体扫描、Creator Memory 写入和原文件操作。

## Independent Acceptance Quality

- [x] 每个 P0 user story 都有可不依赖其他 story 的独立验收。
- [x] success criteria 覆盖恢复率、authority parity、history 诚实性、隐私、diff parity、CAS race、failure fallback、schema drift、legacy guard 和 UI 验收。
- [x] 并发 restore 至少 100 轮、mixed history 至少 50 步、history benchmark 100+100+100 fixture 的数量门槛已定义。
- [x] 已将 benchmark 500 ms 定义为硬目标；交易内 typed ledger attestation 后 p95 从 `1760.719 ms` 降至 `227.083 ms`，并由 profile test 直接断言 `p95 <= 500 ms`。
- [x] 1440×1000、1280×800、390×844 布局与关键状态截图要求已明确。
- [x] 无 console error、broken image、page overflow，键盘/focus/ARIA/触摸目标已纳入验收。
- [x] 已要求 red-before-green、focused + full regression、schema oracle 和独立 P0/P1 复审。

## Implementation Gates

- [ ] 现有 409/legacy-workbench 断点已有当前 diff 的 red test 证据。
- [x] Core/backend canonical workspace 与 3-lane safe projection 已实现。
- [x] Renderer strict normalizer/session/resume 已实现，database/project/scope 不匹配的 stale response 无法覆盖 current。
- [x] Blueprint sections、authority/executable 状态、history lanes、compare/restore UI 已实现。
- [x] exact database/head CAS restore/conflict/lost-response replay 测试通过；100-round oracle 的 silent overwrite 为 0。
- [x] canonical/authority/history privacy/tamper/boundedness 对抗测试通过，包含跨事务、密封对象、替换快照与 TEMP 名称解析反例。
- [x] V1–V5 checksum + normalized V5 schema exact-equal：1/1 durable no-migration oracle 通过，fresh 与 v3→v5 new/missing object 均为 0。
- [x] Blueprint project 的 legacy Timeline create/revise/new render 仍全部阻断，exact legacy-only project 不退化。
- [x] MCP 仍为 read-only annotations，228/228 plugin safe-reader/隐私回归通过。
- [ ] 桌面/移动真实截图、console/overflow 检查和 accessibility 验收已归档。只读 browser 已检查 1440×1000、390×844 和实际 1280×720，无 overflow 且 fresh console clean；截图未 repo-local，完整 keyboard/focus 和 Electron Desktop writable flow 未完成。
- [x] 100+100+100 focused read p95 ≤500 ms。当前冻结 fixture 为 `227.083 ms`，25 个 raw samples 和 fixture contract digest 已记录。
- [x] 当前 final diff 上完整 `npm run check` 通过，exact command/count 已记录。
- [x] 独立复审无未解决 P0/P1/P2，implementation evidence 已仅写实际观察结果。
- [ ] Remote GitHub Actions CI 已运行；若仍未运行，状态必须保持 `NOT RUN`而不勾选。
