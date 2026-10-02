# Tasks: ML-017-A Canonical 1080p Export & Success-only Usage Ledger

- 状态：`IMPLEMENTED IN SHARED WORKTREE / LOCAL VALIDATION WITH RESIDUALS`
- 完成标记规则：`[x]` 只表示当前 shared worktree 已有对应代码/文档表面；不代表命令已由本 evidence 记录通过。最终验证任务在真实观测写入 [implementation-evidence.md](implementation-evidence.md) 前保持未勾选。

## Phase 0 — Baseline and contract freeze

- [x] **T001** 在最终 diff 上记录 branch、base HEAD、package version、dirty worktree、writer Python/SQLite 和 FFmpeg/FFprobe identity。
- [x] **T002** 锁定 legacy 边界：`render_jobs` 绑定 legacy `timelines`，legacy export 不解锁，preview/Verified Save As 不计 successful export/usage。
- [x] **T003** 冻结 ML-017-A 窄合同：`export-1080p`、30 fps、silent、hard cut、四种比例、五个 package roles。
- [x] **T004** 冻结权威流：path-free exact presentation → Electron main native directory selection → one-shot approval；renderer/MCP/Agent 不获得 path/nonce/credential。

## Phase 1 — V8 persistence and success-only ledger

- [x] **T010** 添加 V8 additive migration：`canonical_export_operations/jobs/revisions/receipts` 与 `canonical_usage_occurrences`。
- [x] **T011** 将 V8 table/index/trigger 纳入 migration checksum、physical schema manifest、collision preflight 和 recovery backup，不改 V1–V7 tuple。
- [x] **T012** 添加 immutable operation/revision/usage/receipt triggers，以及 export job identity/terminal/state-transition guards。
- [x] **T013** 实现 trusted V8 ledger validation、bounded protected history/trusted floor 和 path-free canonical JSON/digest checks。
- [x] **T014** 实现 exact current Blueprint/Coverage/full evidence/Timeline/fixed-source export presentation 验证。
- [x] **T015** 实现 user-export root prepared descriptor：比较 Electron selection device/inode，admission 前不写 SQLite locator，命令事务才与 operation/job/receipt 原子物化；不允许把已有其他 kind 的 root 暗中改为 `user_export`，拒绝/replay 不遗留 fd。
- [x] **T016** 实现 `main_native_user_gesture` command、one-shot native nonce、permanent idempotency receipt 和 same-request replay。
- [x] **T017** 实现 operational job get/list/update/cancel-request/interrupted recovery 与 terminal row freeze。
- [x] **T018** 实现发布前 durable commit attestation：一次性固定 exact manifest/artifact/runtime/usage/upstream proofs 并进入 `commit_pending`；物理发布后 one-transaction completion 只接受与先验 attestation exact-equal 的 proof，写 Export Revision + Usage Occurrences，最后将 job 设为 `succeeded`。
- [x] **T019** 实现 attestation-bound `commit_pending` idempotent reconciliation；无 attestation、自洽替换包、failed/cancelled/仍处于 interrupted 的 job 不产生 successful revision/usage，只有恢复成功并转为 `succeeded` 后才产生成功事实。

## Phase 2 — Canonical renderer and lightweight package

- [x] **T020** 实现 canonical Timeline + separate source bindings 到现有 low-level RenderPlan 的路径无关 adapter，不使用 legacy planner/job。
- [x] **T021** 实现四种 aspect ratio 的固定 1080p geometry、30 fps、`cover`、silent、hard-cut validator。
- [x] **T022** 实现 source no-follow descriptor open、identity/size/hash 校验、app-owned frozen snapshot 和 path-free actual-read manifest。
- [x] **T023** 实现 FFmpeg master assembly/probe 与 closed runtime manifest，严格比对 render-derived 与 Core-derived Usage Occurrences。
- [x] **T024** 实现 exact Blueprint script blocks → normalized `script.txt` 投影及 digest proof。
- [x] **T025** 实现五角色 package manifest/human usage/marker；subtitle/cover 固定 absent，runtime/actual reads 内联。
- [x] **T026** 实现 Core-held output-root `dir_fd` 上的 job-owned staging、per-file fsync、marker-last、directory fsync、no-overwrite publish 与 post-publish exact verification；不按 locator 重开目标。
- [x] **T027** 实现 attestation-bound existing package inspection、exact recovery proof 和 deterministic-rejection quarantine；Core success 后不 quarantine，未知/quarantine failure 保持 recoverable；不修改 Library 原件。
- [x] **T028** 添加并运行 persistence/artifact 单元与故障注入测试；执行结果已记录于 evidence。

## Phase 3 — Service, API, Electron and renderer UX

- [x] **T030** 实现 `CanonicalExportService` 与 single-worker `CanonicalExportJobRunner`，按 job-bound Timeline/script/source 运行，不升级 legacy Timeline。
- [x] **T031** 将 runner/service 接入 production runtime、shutdown 和 activation-time interrupted storage reconciliation；terminal write/trusted-read/quarantine 未决使 runner unhealthy，recovery 未闭合则 activation fail closed。
- [x] **T032** 添加 main-only strict presentation/start POST，要求 Desktop authentication、fresh runtime epoch 和 `Idempotency-Key`。
- [x] **T033** 添加 renderer-safe project jobs/exact job/exact successful revision GET 与 stable non-reflective error mapping；revision 可读 Usage Occurrences 与 digests，响应排除 path/root fingerprint/nonce/credential。
- [x] **T034** 在 canonical workspace 同一 trusted read 中投影 `canonical_export` 的 blocked/available/in-progress 状态与 capability。
- [x] **T035** 添加 closed TypeScript export types/normalizers，严格校验 presentation/job/binding/package roles 和 path-free response。
- [x] **T036** 实现 Electron main coordinator：backend trust 重验、native dialog device/inode + held descriptor、one-shot nonce/idempotency key、exact response adoption guard，以及 post-dispatch `unknown` 分类。
- [x] **T037** preload 只暴露 high-level `approveAndExportCanonicalTimeline`，并完成 main IPC/renderer declaration wiring。
- [x] **T038** 在 Blueprint workspace 显示 explicit export/refresh、current limitation、latest job 与五角色包；明示 preview/Save As 不计 Usage，在 `unknown` 后锁住再次批准直到可信 refresh；若 refresh 发现 interrupted，则投影 `blocked / export_recovery_required / capability=false` 并提示重启 MemoLens 触发 activation recovery。
- [x] **T039** 添加并运行 Electron coordinator 和 renderer workspace model 测试；执行结果已记录于 evidence。

## Phase 4 — Verification and evidence closure

- [x] **T040** 在固定 Python 3.14/SQLite runtime 上运行 canonical export persistence/artifact/service/API focused tests，记录 exact counts、duration、warnings 和 exit status。
- [x] **T041** 运行 Electron coordinator/workspace/export model focused Node tests、TypeScript typecheck 和 renderer/Electron build，记录 exact counts。
- [x] **T042** 用 tiny image/video fixtures 完成一次真实 FFmpeg 1080p 导出，验证五个包文件、probe、manifest/marker、DB revision/usage 和原件不变。
- [x] **T043** 执行 pre-attestation/package/publish/post-publish/Core-success-wrapper-failure/unknown/quarantine-failure/cancel/kill/restart 矩阵，确认错误 successful usage 为 0、Core success 包不被 quarantine、unknown 保持 recoverable/unhealthy 且无覆盖。
- [x] **T044** 运行 native selection inode swap/held-fd root replacement/zero-locator rejection、nonce/idempotency/lost-response/concurrency/stale presentation/database-switch/source-race 矩阵。
- [x] **T045** 重跑 V1–V7 migration/checksum、Blueprint/Coverage/Timeline、plugin safe-reader、legacy Timeline/render guard 和 renderer regressions。
- [x] **T046** 在 final diff 上运行完整 `npm run check`，记录 exact component counts、runtime identity、base commit、exit status 和 residuals。
- [x] **T047** 执行独立产品/架构/安全复审，覆盖 main authority、native root inode/dir-fd、legacy isolation、source TOCTOU、durable-attestation recovery、unknown/unhealthy、success-only Usage、隐私和回滚。
- [ ] **T048** 完成真实 Electron 原生目录选择、available/in-progress/success/error/unknown/export-recovery-required 视觉证据、restart guidance 和用户可见包验收。
- [x] **T049** 只按实际观测填写 [implementation-evidence.md](implementation-evidence.md)，再更新 Spec/Plan/Tasks/README/Roadmap 的验证状态。

## Explicitly deferred tasks

- [ ] 字幕、封面、转场、speed/reverse/freeze/nested sequence，以及任何音轨能力（source audio、配乐、旁白、混音）；A 的 master 有意为 silent 且必须无 audio stream。
- [ ] ML-017-B 完整素材包与精确 used fragment 复制/转码。
- [ ] Usage correction/supersession、final/test role 修正、source relink 和 package repair workflow。
- [x] Media Search 的 unused/prefer-unused/allow-reuse/used-in/residual projection 与 Brief admission（由 [ML-017-A2](../017-a2-usage-projection-admission/spec.md) 交付）。
- [ ] Materialized Wiki projection、derivative-output exclusion 与 exact residual clip identity/selection。
- [ ] 自动上传、云同步、社交发布、自动归档/删除/移动原素材。
- [ ] Remote CI、signed/notarized bundle、clean-machine、发布和升级通道验收。

上述 deferred checkbox 在 ML-017-A 通过本地门禁时仍保持未勾选；它们是切片边界，不是 A 的失败。
