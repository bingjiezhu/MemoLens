# Tasks: ML-015-B2B4

- 实施状态：`IMPLEMENTED IN SHARED WORKTREE / PROMOTION BLOCKED`
- 说明：`[x]` 只表示该条已有代码与所列本地证据；`[ ]` 仍是验证缺口。V12 convergence、V13 Timeline history/restore 与最终本地整仓 gate 已完成；真实 fresh 双向 cross-host model/UI journey 仍未完成，所以本切片不得 promotion。

## Contract and failing oracles

- [x] T001 冻结 canonical handoff MCP input/result、Browser session protocol、paired route、actor/origin 和 stable errors。
- [x] T002 冻结 V12 versioned resource-neutral 物理迁移：`agent_project_command_receipts` + 单一 `agent_project_capability_events` sequence，并以历史保存 oracle 证明 v1 digest-only / v2 exact-request union 可行。
- [x] T003 添加 fresh/populated V10→V11→V12→V13、future/checksum/name/DDL collision、copy/swap/backup fault 失败测试。
- [x] T004 添加旧 Blueprint capability/receipt/event ID、JSON、digest、sequence 和关联逐条保存 oracle，并验证 V12/V13 不重签历史。
- [x] T005 添加 pairing action scope、proof substitution、nonce replay、CAS race、partial receipt/use fault 和 secret split-output redaction oracle。
- [x] T006 添加 Codex→DeepSeek 与 DeepSeek→Codex 无聊天上下文 fresh automated adapter-process journey harness（不替代 T053/T054 真实 host/UI 验收）。

## V11 authority, V12 receipt convergence and V13 restore

- [x] T010 实现 V11 capability action closed union，显式加入 `timeline.apply_edit` 并拒绝 wildcard/重复/乱序。
- [x] T011 以 V12 将旧 Blueprint receipt 的 17-column projection/bytes/digests 原样迁入 generic v1 分支，保持 `request_json=NULL`，重定向而不重签 v1 event linkage，并删除旧 authority table。
- [x] T012 实现受限 backup + 单事务 copy/verify/swap migration，不改写 V1–V10 revision/operation/digest history。
- [x] T013 扩展 immutable triggers、indexes、foreign-key/语义 cardinality、resource budgets、cold schema allowlist/prefix commitment 和 warm successor vector。
- [x] T014 在 Core 与 standalone plugin 同步当前 V13 name/checksum/manifest/parser/future refusal，同时显式冻结 V12 validator，添加交叉兼容测试。
- [x] T015 验证旧 V5/V10 paired Blueprint 命令经 V11/V12/V13 后的 replay、tamper detection 和运行时行为不变。
- [x] T016 实现 V13 `timeline.restore_revision` 独立 action 与 closed operation union；restore 复用 generic v2 exact-request receipt，以 current N 为 parent、historical K 为 source 追加 N+1，并对 history bytes/hash 保持和 tamper refusal 建立 oracle。

## Paired Timeline command

- [x] T020 将 `timeline.apply_edit` 与独立 `timeline.restore_revision` 接入 pairing request/status/presentation/approval/revoke/credential action allowlist；edit-only capability 不能 restore。
- [x] T021 抽象 typed paired project-command executor，保持 existing Blueprint bytes/errors/API 兼容。
- [x] T022 实现 `POST /v1/agent/creative/projects/{project_id}/timeline/edit`，复用 B2B3 normalizer/service，不接受 actor/origin/source/result Timeline。
- [x] T023 实现 server-derived paired actor 和 `agent_plugin_editor` origin，永久记录 vendor/user authority false。
- [x] T024 线性化 capability definitive check/use、Timeline operation/revision/head 和 paired receipt，加入 exact successor validator。
- [x] T025 实现 same-request replay、different-request conflict、response-loss reconciliation 与 no-double-use。
- [x] T026 通过 wrong DB/project/action、expiry/revoke/exhaust/restart、source/upstream/head stale、fault rollback 和 cold-tamper 矩阵。
- [x] T027 实现 paired Timeline restore exact K→N+1、stable replay/nonce consumption、generic v2 receipt/event/operation 一一对应和 cold-audit fail-closed。

## Canonical editor server and UI

- [x] T030 新增 project-bound canonical editor session，只从 read-only canonical project reader 导出 current N、read-only historical K 与 source facts。
- [x] T031 新增独立 canonical editor 页面，不向 legacy draft 页面增加隐式 Save。
- [x] T032 接入 B2B3 四种 edit，以及 restore 的 Inspect→Stage→Save K→N+1；pending edit/restore 互斥，保留 Discard 并拒绝额外 legacy 方言。
- [x] T033 让 editor server 内部 credential client 发起 nonce/proof/paired write；验证 Browser 从未取得 secret/token。
- [x] T034 实现 edit/restore Save/replay 后 project refresh + exact Timeline reread 与 full head/selected resource/operation admission，拒绝 optimistic N+1。
- [x] T035 实现 conflict/stale/pairing-required/expiry/revoke/unknown-commit UX；保留 pending 与稳定幂等身份，不自动 rebase、不隐式换素材。
- [x] T036 通过 bootstrap one-use/expiry、session、Host/Origin/CSP/method/content-type/body/TTL/count、无 path/source/secret 泄漏安全矩阵。
- [x] T037 通过 pending noncanonical、Discard、replace server derivation、refresh、process crash before/after commit 状态机测试。

## Codex and DeepSeek adapters

- [x] T040 在 Codex plugin manifest/MCP/prompt/skill/README 公开 canonical handoff 主路径和 exact in-app Browser link 指引。
- [x] T041 在 DeepSeek Harness/Cordis tool registration/skill/README/client card 公开同一 tool 和 **Open MemoLens Canonical Editor** 按钮。
- [x] T042 验证 DeepSeek rich card 和 fallback 不重写 URL，Codex/DeepSeek 都不声称无手势自动打开。
- [x] T043 将旧 `memolens_editor_handoff` 的 UI/tool/prompt/skills/README 全部标记为 **Unsaved Draft Lab / Not saved / process-scoped**。
- [x] T044 验证 legacy editor 不能获得 paired Timeline write、不能导入 canonical state，其已有临时功能仍兼容。
- [x] T045 调整 Electron pairing presentation/revoke 以显示 Timeline action/head；验证完整 edit/save 旅程不要求 Electron Timeline 工作台。

## Verification and promotion

- [x] T050 运行 V11→V12→V13 migration/authority focused tests 和 Core↔plugin schema parity。
- [x] T051 运行 paired Timeline Core/service/API/proof/CAS/idempotency/cold-audit/fault/resource-budget 全矩阵。
- [x] T052 运行 focused canonical editor **16/16**、renderer models **99/99**、plugin V13 receipt/restore parity 与 official DeepSeek loader fresh-profile 验证；这些 focused 证据不替代 T053–T055。
- [x] T052A 新增持久 V13/WAL real-host fixture 与 path/secret-free cold ledger collector；明确 `manual_acceptance.claimed=false`，不替代 T053/T054。
- [x] T052B 以 fixture 启动真实 Electron/managed backend，修复 backend 双模块身份导致的 pairing broker 误拒绝，并只读验证 pending card 与 exact native review presentation；取消 review、未授予 capability，不替代 T053/T054。
- [ ] T053 以 fresh processes 完成 Codex N→N+1、DeepSeek N+1→N+2 旅程并保存 exact proof。
- [ ] T054 以 fresh processes 完成 DeepSeek N→N+1、Codex N+1→N+2 反向旅程并保存 exact proof。
- [x] T055 运行当前最终 diff 的 focused warning-as-error Python、full Python/plugin discovery、Node/renderer/typecheck/build、`npm run check`、最终 `git diff --check`，回填精确计数/runtime，并验证实际 Codex 安装 cache 的 fresh reinstall/内容 parity（aggregate Python 仍有已知 SQLite finalizer warning，不声称整仓 warning-clean）。
- [x] T056 独立复审 V11 current authority、V12 convergence、V13 restore、secret boundary、ledger replay、cross-host single-source-of-truth 和 UI 诚实性；实现级 P0/P1 为 0，但 T053/T054 仍是 promotion evidence blocker。
- [x] T057 回填 focused 与最终整仓 implementation evidence、精确计数、runtime 与 residuals；未运行 Remote CI/release 明示保留。

## Deferred beyond B2B4

- [ ] 跨资源 unified history、通用 undo/redo/branch/merge 和 Agent/UI 交错操作时间线（Timeline revision restore 已由 V13 完成）。
- [ ] split/delete/transition/crop/keyframe/multi-track 等新 canonical edit dialect。
- [ ] subtitle、source audio、BGM、voiceover、mixing 和 final-fidelity preview/render/export。
- [ ] remote editor、多机同步、已验 host vendor identity 和跨 OS 用户 credential 隔离。
