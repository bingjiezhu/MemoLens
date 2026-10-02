# Implementation Plan: ML-015-B2B4

- 实施状态：`IMPLEMENTED IN SHARED WORKTREE / PROMOTION BLOCKED`
- 目标：让 Codex 和 DeepSeek Harness 通过同一 project-bound Browser UI 对 canonical Timeline 执行 B2B3 四种 edit，并以 paired authority 追加同一条 Timeline revision chain。
- 原则：V11 引入 paired Timeline write，V12 已完成 versioned generic receipt convergence，V13 再加入独立 restore action；UI 始终不得成为第二真源。

## Phase 0 — Freeze contracts and failing oracles

1. 冻结 raw MCP tool `memolens_canonical_editor_handoff(project_id)`、handoff result、Browser session protocol、paired route 和 stable errors。
2. 冻结 V11 paired Timeline 路径，并用历史 oracle 判断 resource-neutral 单表收敛是否能保存全部 V5/V10 digest；oracle 已证明后续必须使用 V12 versioned generic union，不能合成 legacy request body。
3. 先写 populated V10→V11、旧 paired Blueprint history preservation、copy/swap fault、schema tamper 和 future-schema 失败 oracle。
4. 先写 pairing action 越权、proof substitution、nonce replay、CAS race、receipt/use partial fault 和 secret leak 失败 oracle。
5. 先写 Codex→DeepSeek 和 DeepSeek→Codex 的无聊天上下文 journey harness，初始应因缺失 canonical handoff/paired Timeline write 而失败。

## Phase 1 — V11 authority path and completed V12 receipt convergence

1. 扩展 capability action closed union，支持显式 `timeline.apply_edit` 和有界的合法 action sets；不接受 wildcard/重复/乱序。
2. V11 曾保留旧 Blueprint v1 receipt table，并新增 generic Timeline v2 receipt；该过渡状态已由 V12 收敛。
3. V12 已将旧 receipt 的 17-column projection/bytes/digests 原样 copy 到 generic `receipt_schema_version=1` 分支；`request_json=NULL` + `digest_only_legacy`，没有逆向伪造正文或重签 event。
4. 在单事务 migration 中 copy/verify/swap、重定向 v1 event foreign key并删除旧 authority table；保存旧 IDs、canonical JSON、digests、sequences 和链接，迁移前产生受限 backup。
5. 更新 immutable triggers、indexes、typed-union cardinality validator、exact normalized-SQL manifest、resource budgets、cold prefix commitments 和 warm successor vector，并同步 Core/standalone plugin 的 V12 合同。

## Phase 2 — Paired canonical Timeline command

1. 在 pairing broker/main presentation/credential client 中加入 exact `timeline.apply_edit` action，显示当前项目和 Timeline head，不改变 semantic-confirmation 边界。
2. 将 Blueprint-only paired executor 抽象为 typed project-command executor，但保持已有 Blueprint API/receipt/replay bytes 和错误兼容。
3. 新增 `POST /v1/agent/creative/projects/{project_id}/timeline/edit`，复用 B2B3 request normalizer 和 `TimelineLoweringService.apply_edit`，不复制 edit 逻辑。
4. 在同一 verified transaction 中完成 capability definitive check、Timeline operation/revision/head、paired receipt/use event 和 exact successor validation。
5. 增加 server-derived `paired_agent` actor 与 `agent_plugin_editor` origin，保持 vendor/user authority false；拒绝 caller-supplied actor/origin/authority。
6. 实现 same-request replay、different-request conflict、response-loss reconciliation、revoke/expiry/exhaust/restart 和事务 fault matrix。

## Phase 3 — Canonical browser editor

1. 在现有 loopback editor server 安全原语上建立独立 canonical session type和独立页面；不让 legacy draft session 获得 Save 能力。
2. handoff 只根据 `project_id` 通过 canonical reader 导出 exact database/project/Blueprint/Coverage/Timeline/freshness 和 same-Beat alternatives。
3. 在 Browser 实现 B2B3 四种 edit、pending preview、Discard、Save as N+1、conflict/stale/pairing-expired 状态；不加入其他 legacy 操作。
4. Save 由 editor server 内部 credential client获取 nonce/proof；Browser 只提交 closed edit 和 session-bound precondition identity。
5. Save 成功/重放后通过 canonical reader 重读，验证 full head/selected resource/operation。任何 mismatch 不显示“Saved”。
6. 复用并动态验证 one-time bootstrap、session credential、Host/Origin/CSP/method/content-type/body/TTL/session-count 和无 secret/path 泄漏边界。

## Phase 4 — Codex and DeepSeek host wiring

1. 在 Codex plugin manifest/prompt/skill/README 公开 canonical handoff 主路径，要求使用 exact `uiHandoff.url` 并由用户手势在 in-app Browser 打开。
2. 在 DeepSeek Harness/Cordis skill/README/client card 映射同一 raw tool，展示 **Open MemoLens Canonical Editor** 按钮与 exact fallback link。
3. host-specific JS 不复制 project reader、edit 或 paired write；同一 fixtures 验证两个 host 对 tool result 的解释一致。
4. 将现有 `memolens_editor_handoff` 的所有用户可见描述收口为 **Unsaved Draft Lab / Not saved / process-scoped**，保留兼容工具名。
5. Electron 只增加/调整 pairing presentation 中的 Timeline action/head 展示和 revoke，不新建 Electron canonical editor 主界面。

## Phase 5 — Verification and evidence

1. 运行 V11→V12 convergence 与 V12→V13 restore fresh/populated/future/tamper/fault migration 矩阵，并逐条对比旧 Blueprint 与 Timeline paired history。
2. 运行 paired Timeline Core/service/API/proof/CAS/idempotency/cold-audit/resource-budget 矩阵。
3. 运行 editor server 安全、UI 状态机、Codex plugin 和 DeepSeek Harness parity tests。
4. 以全新进程完成 Codex N→N+1、DeepSeek N+1→N+2 及反向 journey；保存 exact database/project/head/operation/receipt 证据。
5. 验证 legacy Draft Lab 在重启后丢失临时修订且 canonical ledger cardinality 不变。
6. 运行 Python warning-as-error、plugin suite、Node/renderer/typecheck/build、diff check 和 final repository gate。
7. 只在证据完整后回填 implementation evidence、tasks、Spec index/roadmap 和状态；本地结果不冒充 Remote CI/release。

## Rollback

- canonical handoff 和 host wiring 是新增入口；禁用新 tool 后 legacy Draft Lab 仍可以只作不保存试剪。
- paired Timeline action 可停止新 issuance，但已完成 V11/V12/V13 receipt/revision 必须继续被 reader/cold audit 验证，不得降级改写。
- V12 migration 失败在 commit 前回滚并保留受限 V11 backup；已成功迁移的数据库不做原地 V11 downgrade。
- legacy request 正文不可恢复已是冻结事实；V12 只能使用 versioned digest-only v1 分支，不在运行时放宽验证、合成证据或临时写 desktop receipt。
- 无法证明 capability/receipt/use/Timeline 事务连续性时，功能必须 fail closed，不退回为“先保存再补账”。
