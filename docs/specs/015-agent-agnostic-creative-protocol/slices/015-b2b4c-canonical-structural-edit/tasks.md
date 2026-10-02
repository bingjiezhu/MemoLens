# Tasks: ML-015-B2B4C

- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCALLY VALIDATED / PARENT B2B4 REAL-HOST JOURNEYS PENDING`
- 说明：仅勾选已有 source 和已观察 evidence 能直接证明的项。production inventory/oracle、final-version plugin discovery、fresh Codex reinstall/cache parity、official DeepSeek loader fresh-profile 与 final-diff 本地全仓门禁已通过；Remote CI/release 与 B2B4 T053/T054 真实宿主旅程仍未闭合。

## Contract and failing oracles

- [x] T001 冻结独立 `timeline.apply_structural_edit/v1` action、effect、scope、request/result、routes、actor/origin 与 stable errors。
- [x] T002 冻结 structural closed parser，仅允许每次一个 `split_clip` 或 `delete_clip`，拒绝顶层 `edit`、额外字段、bool/float、bulk array、caller result/source/actor/origin。实际顶层请求字段为 `structural_edit`。
- [x] T003 冻结 Timeline schema v2 occurrence shape、v1/v2 transitions 与 arbitrary v2 cold-replay refusal。
- [x] T004 冻结 video split exact interval、minimum 100ms、maximum 256 clips、stable child IDs、binding duplication 与 total-duration invariants。
- [x] T005 冻结 delete exact target/binding removal、not-last、ripple timing 与 source media immutability invariants。
- [x] T006 冻结 export duplicate-evidence consistency、usage occurrence、preview lease 和 mixed v1/v2 restore oracles。
- [x] T007 冻结 canonical/Draft Lab static and runtime isolation oracle，明确拒绝 `offset_ms`、generic operations 与 `revise_timeline_draft`。

## Core structural edit and schema v2

- [x] T010 新建独立 Core structural contract；普通 `_EDIT_FIELDS` 继续只含四种 existing edits。
- [x] T011 实现 video-only split，从 verified parent/source bindings 派生左右 child、stable IDs 和 exact v2 result。
- [x] T012 实现 delete occurrence，从 verified parent 派生 remaining clips/bindings 和 deterministic ripple result。
- [x] T013 实现 dual v1/v2 Timeline validator，保持 v1 一一对应与 v2 clip ID/source proof constraints。
- [x] T014 让 move/trim/set-duration/replace 在合法 v2 head 上工作并保持 v2，不扩大普通 edit capability。
- [x] T015 扩展 cold ledger audit，分别 exact replay ordinary edit、structural edit 与 restore；tampered result/source/proof fail closed。除 revision interval/binding/proof tamper 外，还覆盖 operation result delete target 篡改，以及 operation + desktop receipt 自洽重签到另一 delete target 后仍由 causal replay fail closed。
- [x] T016 验证 deterministic replay、ID collision、edge split、unsupported media、clip cap、last delete 和 unknown target。child==child、child 撞 untouched clip、child 复用被 split parent clip ID 均在 parent mutation 前返回 `split_identity_collision`。

## V18 migration and schema parity

- [x] T020 新增 immutable V18 migration，扩展 `canonical_timeline_revisions` 为 schema `1 | 2`。
- [x] T021 扩展 operations、capabilities、command receipts、capability events 的 closed CHECK/typed unions，加入 structural action。
- [x] T022 实现受限 backup + atomic copy/verify/swap，保留所有 V1–V17 rows/JSON/digests/receipts/events/hashes/sequences/foreign keys。
- [x] T023 更新 immutable triggers、indexes、semantic cardinality、resource budgets、cold prefix commitments 和 warm successor vector。
- [x] T024 同步 Core 与 standalone plugin 的 V18 name/checksum/normalized SQL/manifest/parser/future refusal。
- [x] T025 把现有用 schema 18 模拟 future version 的 Core/plugin fixtures 推进到 19，并增加真正的 V17→V18 fresh/populated oracle。
- [x] T026 验证 migration fault、name/DDL collision、tamper、future schema 与 backup permissions 全部 fail closed。

## Authority, service and APIs

- [x] T030 将 structural action 接入 pairing request/status/presentation/approval/revoke/credential allowlist，拒绝 wildcard/隐式继承。
- [x] T031 在 Timeline service 实现 desktop `apply_structural_edit` transaction，共用 Core derivation。
- [x] T032 实现 paired structural endpoint，绑定 exact action/path/body/nonce/HMAC/project/head/idempotency。命令顶层字段为 `structural_edit`。
- [x] T033 保持 server-derived paired actor 和 `agent_plugin_editor` origin；永久 provenance 链接 capability use、receipt、operation、revision 与 head。
- [x] T034 线性化 capability definitive check/use、CAS、operation/revision/head/receipt/event 和 exact successor admission。
- [x] T035 实现 same-request replay、different-request conflict、no-double-use 与 response-loss reconciliation。
- [x] T036 验证 concurrent CAS single winner，以及 stale DB/project/head/Blueprint/Coverage/source 在任何 mutation 前失败。
- [x] T037 在 operation/revision/receipt/use/head 各阶段注入 fault，证明事务零部分写入并可 cold reopen。
- [x] T038 验证 edit-only/restore-only/preview-only deny，structural-only allow；revoked/expired/exhausted/restart/wrong-secret/nonce/origin/path/body/project deny。

## Restore, export, usage and preview

- [x] T040 扩展 history/restore contract/service/readers，支持 mixed v1/v2 chain 和 schema-preserving K→N+1。
- [x] T041 验证 v2 split/delete revision 可 restore；structural-only 不隐式拥有 restore；historical v2 tamper fail closed。真实链覆盖 split rev2→restore rev4，以及 delete-produced rev5→advance rev6→restore rev5 as rev7。
- [x] T042 修改 export actual-read validator，接受 proof-consistent duplicate evidence occurrences，拒绝 evidence→asset/source/proof substitution。
- [x] T043 验证 split 输出两个 unique clip occurrences/exact half-open intervals，delete 后新 export/usage 不包含 target，历史 export revision 仍保持不可变。
- [x] T044 验证合法 repeated source overlap 不被 blanket rule 错拒，错误 range/duplicate clip ID 仍被拒绝。
- [x] T045 Stage structural edit 时撤销旧 preview lease；Discard 和 Save+reread 后按 current/new clip scope 重签。
- [x] T046 验证 Browser 无法扩大 preview scope，旧 child/removed clip lease 不可继续读取。单体真实 route 纵向覆盖 Split→Delete、旧 lease GET/HEAD expiry、新 head 对 deleted child scope denial 与 surviving child 正常读取。

## Canonical Editor and adapters

- [x] T050 在共用 `canonical-editor.html` 增加 selection、playhead Split 和 Remove controls，不修改 legacy editor authority。
- [x] T051 实现 video/minimum-boundary/clip-cap/last-clip/pending/history/capability 的精确 enable/disable 状态。
- [x] T052 实现 playhead→absolute `source_split_ms` 映射和 server-side current-head revalidation。
- [x] T053 保持 Stage/Discard/Save；pending 明示 noncanonical，Save 后 canonical exact reread，conflict/unknown commit 不自动 rebase。
- [x] T054 保持 credential/nonce/proof/idempotency identity 在 editor server，Browser 不取得 path/source secret/actor/origin authority。
- [x] T055 更新 Codex plugin manifest/prompt/skill/README，描述同一 structural handoff 和权限边界。
- [x] T056 更新 DeepSeek Harness skill/README/client card，复用 exact same handoff URL 与 server，不复制 edit/client logic。
- [x] T057 通过 bootstrap one-use/expiry、session、Host/Origin/CSP/method/content-type/body/TTL/count 和 no-secret/path exposure 安全矩阵。
- [x] T058 通过 Codex/DeepSeek controlled shared-UI fixture：split/save N→N+1，delete/save N+1→N+2，并检查 ledger/receipt/head。

## Electron/native compatibility

- [x] T060 扩展 Timeline TypeScript types/models/API/readers，读取合法 v2 duplicate/missing occurrences 和 mixed history。
- [x] T061 更新 Electron authority coordinator，显示/批准/revoke exact structural action，不把普通 edit 显示为 delete authority。
- [x] T062 验证 native reader 对 Browser 保存的 v2 head 不崩溃、不降级、不改写；Export authority 仍只读 current canonical head。
- [x] T063 明确 Electron Split/Remove buttons 可 deferred，且不把缺少 native buttons 伪装成缺少 Browser canonical UI。

## Unsaved Draft Lab isolation

- [x] T070 静态验证 canonical code/page/docs 不出现 legacy `offset_ms`、bulk operations、draft ID 或 `revise_timeline_draft` 调用。
- [x] T071 运行 Draft Lab process-restart test，证明临时 split/delete 状态消失。
- [x] T072 比较 Draft Lab 前后 canonical table-name sentinel SQLite 的 exact bytes/rows/cardinality/digests，证明零旁路 mutation；不把该 sentinel 冒充真实 Core canonical schema。
- [x] T073 保持 legacy image/audio split 仅在 process-scoped Draft Lab；canonical parser 坚持 video-only。

## Verification and promotion

- [x] T080 运行 pure contract/schema/migration/service/API/authority/CAS/idempotency/fault/cold-audit focused warnings-as-error suites：仓库组合 58/58、plugin editor 组合 49/49；独立 structural contract/backend 19/19，均通过且 `ResourceWarning` 升格为错误。
- [x] T081 运行 restore/export/usage/preview/native-reader/editor/security/host-parity focused suites。
- [x] T082 更新并运行 production surface oracle/inventory，确认 standalone plugin 与 Core closed dialect/schema parity：170 actions / 141 exact oracles，141/141 pass。
- [x] T083 运行 plugin manifest validator、fresh reinstall、source↔installed-cache parity 和 official DeepSeek loader fresh-profile verification：Codex installed/enabled `0.10.1+codex.20260830073123`，checksum 零差异；fresh `DSH_HOME` add/list/dump 各 exit 0。
- [x] T084 运行 final-diff full repository gate：第一轮发现并修正两个 V18 过时测试预期；第二轮 `npm run check` exit 0，观察到 Python 1003/1003、plugin 404/404、Node/renderer 120/120；tracked `git diff --check` exit 0，270 个 untracked 文件的 no-index whitespace gate 在机械修复 10 个 EOF 空行后 exit 0。
- [x] T085 独立复审 authority inflation、V1–V17 preservation、v2 replay proof、export substitution、Draft Lab isolation 和 UI honesty；当前实现级 P0/P1 为 0。
- [x] T086 仅按 observed commands/results 回填 implementation evidence、精确 observed counts 与 residuals；第二轮 Python runtime 明示未捕获，未运行 Remote CI/release 明示保留。
- [x] T087 确认 B2B4 T053/T054 真实 Codex→DeepSeek 与 DeepSeek→Codex fresh host/model/UI journeys 继续独立未被替代；本切片不能代勾。

## Deferred beyond B2B4C

- [ ] image/audio split、transition、crop、keyframe、speed、multi-track 和 compound/bulk structural edits。
- [ ] 删除 source media/evidence/Coverage/Blueprint content。
- [ ] 完整 cross-resource unified history、undo/redo/branch/merge。
- [ ] Electron 作为首要完整剪辑工作台。
- [ ] 字幕、source audio、BGM、voiceover、mixing、final-fidelity preview/render/export。
