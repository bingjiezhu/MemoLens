# Requirements Checklist: ML-015-B2B4C

- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCALLY VALIDATED / PARENT B2B4 REAL-HOST JOURNEYS PENDING`
- 说明：已勾选项只表示当前 source 与已观察 evidence 直接支持；未勾选项保留真实宿主、Remote CI/release 或明确 deferred 的产品范围。

## Authority and closed dialect

- [x] `timeline.apply_structural_edit` 是独立 project-bound durable mutation action，不由 edit/restore/preview capability 隐式继承。
- [x] 既有 `timeline.apply_edit` 继续只授予 move/trim/set-duration/replace，不因本切片发生权限膨胀。
- [x] structural request 的顶层字段为 `structural_edit`，每次只接受一个 exact `split_clip` 或 `delete_clip`，拒绝顶层 `edit`、generic operations array 和 caller result Timeline。
- [x] parser 拒绝 unknown/missing fields、bool/float、legacy `offset_ms`、caller source/actor/origin/path。
- [x] paired path 的 action/path/body/project/head/nonce/HMAC/idempotency 全部 exact-bound，Browser 不取得 secret。

## Split and delete semantics

- [x] Split 第一版只支持 video，absolute `source_split_ms` 严格位于 source interval 内。
- [x] Split 两个 child 各至少 100ms，结果最多 256 clips，source ranges 构成 exact half-open partition。
- [x] Split child IDs 由 Core 确定性生成、全局唯一且与 Browser/host/path/randomness 无关。
- [x] Split 保留 Beat/assignment/evidence/asset/proof，复制为两个 exact clip-bound source bindings，总 Timeline duration 不变。
- [x] Delete 只移除选中 occurrence 与 binding，禁止删除最后一个 visual clip。
- [x] Delete 确定性 ripple ordinal/timing 与 duration，且 source media/evidence/Coverage/Blueprint 不被删除或修改。

## Timeline schema v2 and migration

- [x] Timeline schema v1 的 clip/Beat/assignment/evidence 一一对应和唯一性没有被放宽。
- [x] Timeline schema v2 允许 proof-consistent duplicate occurrence 与 Coverage item 的 zero occurrence，同时保持 clip ID 全局唯一、1..256 clips 和一 clip 一 binding。
- [x] 每个 v2 remaining occurrence 仍映射 pinned Coverage 并通过 exact asset/source/proof validation。
- [x] arbitrary v2 不能只凭 shape 入账，必须由 parent operation ledger exact replay 证明。
- [x] ordinary edit 的 v1→v1、v2→v2，以及 structural edit 的 v1/v2→v2 transition 均已验证。
- [x] V18 显式扩展 revisions/operations/capabilities/receipts/events closed schema，并保持 V1–V17 immutable history。
- [x] V18 fresh/populated/backup/fault/collision/future/tamper tests 与 Core/plugin schema parity 已通过。

## Transaction, replay and restore

- [x] project/head/Blueprint/Coverage/source/capability 在同一 verified transaction 中重读。
- [x] operation/revision/head/receipt/use/event/CAS 全部成功或全部回滚。
- [x] same-request replay 零额外 rows/uses；different-request conflict；concurrent write 仅一位 CAS winner。
- [x] stale、revoke、expiry、exhaustion、restart、wrong secret/nonce/origin/project/path/body 在 mutation 前 fail closed。
- [x] cold audit 可重放 ordinary/structural/restore mixed ledger，并发现 child interval/binding/proof/delete-target tamper；即使 operation 与 desktop receipt 被自洽重签到另一 delete target，causal replay 仍 fail closed。
- [x] restore 可在 v1/v2 mixed history 中 append K as N+1，并精确保留 K Timeline schema/source bindings。
- [x] structural capability 不授予 restore；restore capability 不授予 structural edit。

## Export, preview and consumers

- [x] export actual-read admission 接受同 evidence 的 proof-consistent repeated occurrences，拒绝 asset/source/proof substitution。
- [x] split 的两个 usage occurrences 保留 unique clip IDs 与 exact half-open intervals；delete occurrence 从新 export/usage 消失，旧 export revision 仍保留历史 occurrence。
- [x] 合法 source overlap 不被 blanket rule 错拒，duplicate clip ID/错误 interval 继续拒绝。
- [x] structural Stage 撤销旧 preview lease；Discard/Save+reread 后按 current/new clip scope 重签；真实 Split→Delete route 证明旧 deleted-child lease GET/HEAD expiry、新 head 对 deleted child scope denial、surviving child 可读。
- [x] React/Electron readers 接受合法 schema v2、duplicate assignment/evidence 和 mixed v1/v2 history，不改写或降级 canonical head。
- [x] Electron authority presentation 准确显示/批准/revoke structural action；Electron 编辑按钮不是主验收门槛。

## Shared UI and Draft Lab isolation

- [x] Codex 与 DeepSeek Harness 使用同一个 `canonical-editor.html`、editor server、handoff URL 和 Core contract。
- [x] Canonical UI 提供 playhead Split、Remove、Pending/Discard/Save，并在 Save 后 exact reread。
- [x] Split/Delete enablement 对 media kind、100ms boundary、256 cap、last clip、history、pending 和 capability 精确 fail closed。
- [x] UI 明示 Remove 只移除 Timeline occurrence、原始媒体不变；pending 明示 noncanonical。
- [x] conflict/stale/unknown commit 不乐观显示 Saved、不自动 rebase、不隐式换 source。
- [x] Canonical implementation 不 import/call `revise_timeline_draft`，不接受 draft ID/Timeline/bulk/`offset_ms`。
- [x] Draft Lab restart 丢失临时状态，且其前后 canonical table-name sentinel SQLite exact bytes/rows/digests 不变；该 sentinel 不冒充真实 Core canonical schema。

## Evidence and promotion

- [x] focused contract/schema/migration/service/security/restore/export/preview/UI suites 已在 exact implementation diff 上通过：仓库组合 58/58、plugin editor 组合 49/49，均以 `ResourceWarning` 升格为错误。
- [x] production inventory/oracle、plugin validator、fresh reinstall/cache parity、DeepSeek loader parity 已通过。
- [x] full Python/plugin/Node/renderer/typecheck/build、`npm run check`、tracked 与全量 untracked whitespace gates 已通过；记录 exact observed counts，未捕获的 Python runtime 明示为未捕获。
- [x] 独立 review 的实现级 P0/P1 为 0，未运行 Remote CI/release 与 residuals 均明确记录。
- [x] B2B4 T053/T054 真实 Codex→DeepSeek 与 DeepSeek→Codex fresh host/model/UI journey 继续独立保留；本切片证据没有代替或代勾。
