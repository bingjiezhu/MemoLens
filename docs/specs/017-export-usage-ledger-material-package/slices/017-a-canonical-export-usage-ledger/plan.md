# Implementation Plan: ML-017-A Canonical 1080p Export & Success-only Usage Ledger

- 状态：`IMPLEMENTED IN SHARED WORKTREE / LOCAL VALIDATION WITH RESIDUALS`
- 规范：[spec.md](spec.md)
- 原则：Canonical Timeline 决定“读什么”，Electron main 持有的原生目录身份决定“写到哪里”，发布前 Core attestation 与其后 exact physical proof 共同决定“何时写 Usage”。

## 1. Architecture decision

```text
Renderer workspace
  → GET canonical workspace/job projections only
  → high-level approveAndExportCanonicalTimeline(project, suggestedName)

Electron main
  → POST main-only presentation with strict {}
  → validate path-free exact project/Timeline/Blueprint/Coverage/profile/roles
  → native directory selection; O_NOFOLLOW open; hold device/inode descriptor
  → fresh backend trust check
  → one-shot nonce + Idempotency-Key
  → POST main-only export command with exact presentation, destination and selection identity

Core V8 command transaction
  → validate V8 ledger + exact current upstream/source state
  → compare the native-selected device/inode and prepare a held descriptor
  → consume one-shot nonce
  → atomically materialize root locator + immutable operation + operational job + permanent receipt

Recoverable single-worker outbox
  → revalidate exact job-bound Timeline/Blueprint/source
  → open output through the Core-held root descriptor; never reopen by locator
  → no-follow open + identity/hash check + app-owned frozen snapshots
  → FFmpeg 1080p silent hard-cut master
  → durable immutable commit attestation; job = commit_pending
  → stage five-role lightweight package; marker last; fsync
  → no-overwrite directory publication
  → physical package verification against the prior attestation
  → one DB transaction: Export Revision + Usage Occurrences + job succeeded

Activation recovery
  → mark nonterminal jobs interrupted
  → load durable Core attestation before inspecting package bytes
  → exact package reconciliation or proven deterministic rejection quarantine
  → unknown/quarantine failure remains recoverable; runner unhealthy; activation fails closed
```

这是不能假设具有跨介质原子性的提交协议：SQLite 先持久化 immutable commit intent，用户文件系统随后 publication，SQLite 最后写 successful ledger。`commit_pending` 从发布前 attestation 一直跨越到物理验证后的 success transaction；恢复不能从包内自洽内容反向发明 intent。

## 2. Authority and public contract

### Main-only commands

- `POST /v1/main/creative/projects/{project_id}/exports/presentation`：严格空 JSON object，返回 path-free exact presentation envelope。
- `POST /v1/main/creative/projects/{project_id}/exports`：只接受 exact presentation/digest、main-created native gesture nonce 与 native-selected destination；必须带 `Idempotency-Key`。

Main coordinator 是唯一能读 backend main credential、打开原生目录选择、生成 nonce 和传递 canonical path 的进程。选择后 main 以 no-follow 方式打开目录，固定 device/inode 并持有 handle，直到 Core 完成身份比较和命令 admission。Preload 只暴露高层 `approveAndExportCanonicalTimeline`；它不暴露任意 HTTP、目录、nonce 或 token surface。已发出命令但响应无法验证时，coordinator 返回 `unknown`，renderer 锁定再次批准；可信 canonical workspace refresh 只负责重新分类。若 latest job 为 interrupted，仍保持 blocked 并提示重启 MemoLens 运行 activation recovery，不能仅凭 refresh 消解。

### Renderer-safe reads

- project export job list / exact job GET 只返回状态、精确 Timeline binding、package basename 和成功 proof；exact successful Export Revision GET 额外返回无路径 output/script/package/usage digests 与 Usage Occurrences。三者都不返回 root locator/fingerprint。
- canonical workspace 加入 `canonical_export` summary：`blocked / available / in_progress`，理由限于 `timeline_missing / timeline_stale / requires_native_confirmation / export_in_progress / export_recovery_required`。Latest job 为 `interrupted` 时必须是 `blocked / export_recovery_required / capability=false`；refresh 只重新分类，用户需重启 MemoLens 才能运行 activation recovery gate。
- `capabilities.canonical_export` 仅在 Timeline current 且既没有 active job、也没有 interrupted/recovery-required job 时为 true；成功的 latest job 可显示，但下一次导出仍要新的原生批准。

## 3. V8 data model

V8 是 additive migration，不更改 legacy Timeline/render rows：

- `canonical_export_operations`：immutable `export.create` command，固定 presentation、上游 bindings、root permission binding、profile、job 和 request digest。
- `canonical_export_jobs`：operational outbox，closed state machine 为 `requested → rendering → packaging → commit_pending → succeeded`，并有 `cancelling/cancelled/failed/interrupted` 恢复路径；`commit_pending` 同时持有一次写入的 path-free commit attestation，固定预期 package/Usage/runtime/physical proofs；identity 与 attestation 不可改，终态冻结。
- `canonical_export_revisions`：只为 successful job 创建，固定 exact Timeline/Blueprint/Coverage、output/script/package/usage/runtime/marker digests 和 completed time。
- `canonical_usage_occurrences`：按 export revision + ordinal append-only，固定 clip、asset/source、Timeline 与 video source interval。
- `canonical_export_receipts`：按 database + `main_native_user_gesture` + project + command + idempotency key 永久保留 replay result。

所有 immutable rows 都进入 physical schema manifest、trigger 保护、bounded full-ledger validation 和 trusted-floor 检查。V1–V7 migration tuple/checksum 不允许被 V8 改写；同名 table/index/trigger collision 或 physical schema drift 必须在领域写入前 fail closed。

Usage 不额外存一个“已用文件”布尔头。每次 occurrence 是事实，source-level used union 和 residual 是将来由投影层从 occurrences 与 source domain 合并/差集得到的可重建结果。

## 4. Artifact pipeline

### Canonical render adapter

`backend/src/media/canonical_export_artifacts.py` 将 canonical Timeline document 与分离的 source-binding manifest 转换为现有 low-level `RenderPlan`，但不调用 legacy planner/job ledger。规则固定为：

1. 一个 canonical clip 对应一个 render clip 和一个 Usage Occurrence。
2. Timeline timing 原样保留；video source range 必须与 binding 一致，image 不伪造 source time。
3. 四种 aspect ratio 对应固定 1080p geometry，30 fps，`fit=cover`，无转场、无字幕，且有意 silent；master probe 必须证明不存在 audio stream。
4. 先用 no-follow descriptor 验证 source identity/hash，再复制到 job temp workspace；FFmpeg 只读 frozen snapshot，以关闭 hash-check 与 path reopen 之间的竞态。
5. master probe、runtime identity、source snapshot proof 和 actual-read manifest 必须通过 closed/path-free validator。

### Five-role package

固定顺序的 package roles 是：

1. `final_video` → `video.mp4`
2. `script` → `script.txt`
3. `package_manifest` → `manifest.json`
4. `human_usage_list` → `使用清单.txt`
5. `completion_marker` → `.memolens-complete.json`

`script.txt` 从 exact Blueprint script blocks 投影；空、非规范化或过大脚本拒绝。Manifest 将 subtitle/cover 固定为 absent，并内联 Usage Occurrences 与 runtime/actual-read manifests，而不多造 sidecar 真源。

发布算法为：先持久化 Core commit attestation，再沿批准并由 Core 持有的 output-root `dir_fd` 创建 job-owned hidden staging，逐文件 no-follow 写入、hash、fsync，最后写 completion marker 并 fsync 目录，然后用 no-replace rename 发布。发布后继续通过同一 `dir_fd` 重验 exact package 与 attestation。已有同名目录始终是 conflict，不提供 overwrite/update mode。

恢复分类不是“任何错误都 quarantine”：durable Core success 始终保留已成功包；只有可信 job 读取证明 deterministic rejection 且 exact attestation 仍能识别物理包时才 quarantine；DB 结果未知、trusted read 失败或 quarantine 失败保持 interrupted/recoverable，并把 runner 标为 unhealthy。presentation/start 会 fail closed，activation recovery 未闭合时新 runtime 不可见。

## 5. Components

- `core/media_db.py`：V8 schema/manifest/migration、presentation/envelope、prepared native root descriptor、atomic command/receipt、job state machine、durable commit attestation、Export/Usage admission、trusted reads/reconciliation。
- `core/db.py`：schema version 与 compatibility integration。
- `backend/src/media/canonical_export_artifacts.py`：fixed render adapter、frozen source reads、runtime proof、five-role package、inspection/quarantine。
- `backend/src/media/canonical_export.py`：main command service、renderer-safe projection 和 single-worker recoverable outbox。
- `backend/src/api/routes.py` / `backend/src/__init__.py`：main/read routes（包含 exact successful revision）、runtime wiring、shutdown/interrupted reconciliation。
- `electron/canonicalExportCoordinator.ts`：exact presentation review、native destination device/inode + held handle、one-shot approval、post-dispatch unknown 分类和响应身份检查。
- `electron/main.ts` / `electron/preload.cts` / `src/electron.d.ts`：main IPC 与唯一高层 preload surface。
- `src/blueprint/exportTypes.ts` / `exportModel.ts`：closed renderer-safe types/normalizers。
- `src/blueprint/BlueprintProjectWorkspace.tsx`：available/in-progress/blocked 状态、限制说明、explicit export/refresh、unknown lock，以及 interrupted=`export_recovery_required` 的 restart guidance。
- `tests/test_canonical_export_persistence.py`、`tests/test_canonical_export_artifacts.py`、`tests/canonical_export_coordinator.test.mjs` 及 workspace/API regression：实现门禁候选，实际结果只记入 evidence。

## 6. Phases and gates

### Phase 0 — Baseline and red oracles

1. 锁定 branch/base/runtime/SQLite/worktree 和 V1–V7 migration oracles。
2. 证明 legacy export 仍为 403，legacy `render_jobs` 仍外键绑定 legacy Timeline，Electron Save As 只复制 preview。
3. 新增 red tests：无 canonical export presentation/main command/V8 ledger，preview 不产生 Usage。

**Gate P0**：基线快照和环境阻断分开记录。

### Phase 1 — V8 ledger and native command

1. 添加 V8 additive migration、collision preflight、backup/recovery 和 physical schema verification。
2. 实现 exact path-free presentation、one-shot native envelope、selection device/inode binding、prepared descriptor 与 root locator/operation/job/receipt atomic command；任何拒绝/replay 都不遗留 prepared authority。
3. 实现 job state machine、terminal immutability、same-request replay 和 bounded trusted ledger read。
4. 实现发布前 durable commit attestation 与 success-only revision/occurrence completion；物理 package proof 必须与先验 attestation exact-equal，不能以 caller 或 package 自洽内容替代。

**Gate P1**：fresh V8、V7→V8、collision、tamper/rollback、nonce/idempotency/CAS/fault tests 通过；失败 job 的 revision/usage 为 0。

### Phase 2 — Frozen render and package commit

1. 实现 canonical Timeline 到 1080p silent hard-cut RenderPlan 的纯 adapter。
2. 实现 source descriptor/hash/snapshot 闭环和 path-free actual-read/runtime proof。
3. 实现 exact Blueprint script projection、人类 Usage 清单和 closed manifest。
4. 实现 Core-held `dir_fd` 上的 marker-last/fsync/no-overwrite package publication、attestation-bound inspection，以及仅用于 proven rejection 的 deterministic quarantine。

**Gate P2**：图片/视频/四比例、source/output-root race、pre-attestation crash、自洽替换包、全阶段 fault、同名 conflict 和中断 package 恢复通过；运行时 manifest 不泄露路径，未知结果不被误写为 failure/success。

### Phase 3 — Service, API, Electron and workspace

1. 接入 service/runner/runtime activation/shutdown/reconciliation。
2. 添加 main-only presentation/start 和 renderer-safe job reads，保持 strict JSON/stable errors。
3. 实现 Electron coordinator、native dialog held identity、preload one-shot API、backend trust recheck，以及 post-dispatch `unknown` 结果分类。
4. 将 canonical export state/capability 接入 Blueprint workspace，显示 1080p/silent/hard-cut 限制和五角色包；unknown 时锁住再次批准直到可信 refresh。Refresh 若确认 interrupted，则继续 blocked 并提示重启 MemoLens，不把 interrupted 当成可重试 idle。

**Gate P3**：renderer 不能获得 path/nonce/token，取消原生对话零写，stale presentation/database restart/forged response fail closed，legacy preview/export 不被升级。

### Phase 4 — Final-diff verification and evidence

1. 运行 focused persistence/artifact/service/API/coordinator/workspace tests，记录 exact counts 和 warning policy。
2. 用 tiny image/video fixtures 运行一次真实 FFmpeg 导出，解析五个包文件、成片 probe、manifest、Usage rows 和恢复路径。
3. 重跑 Blueprint/Coverage/Timeline、V1–V7 migration、plugin safe reader、legacy Timeline/render guard 和 renderer/Electron regressions。
4. 在 final diff 上运行 `npm run check`，记录 runtime/base/dirty state/counts/exit status/residuals。
5. 独立复审 authority、source race、two-commit recovery、success-only Usage、legacy isolation、路径隐私和不实声明。

**Gate P4**：所有当前 P0/P1 问题关闭，本地验证与 Electron 视觉/Remote CI/release 证据边界按实际状态写入 [implementation-evidence.md](implementation-evidence.md)。

## 7. Test matrix

| Domain | Positive oracle | Failure/adversarial oracle |
| --- | --- | --- |
| Authority | exact path-free presentation → held device/inode selection → one-shot command | renderer direct path, selection inode swap, reused nonce, missing main credential, backend restart, stale presentation rejected with zero locator/job side effect |
| Upstream truth | current Blueprint/Coverage/evidence/Timeline/source accepted | any head/digest/source drift, rollback/tamper, legacy fallback rejected |
| Render | image/video hard-cut master in four 1080p geometries | transition/audio/subtitle/speed/nested/unknown field rejected |
| Source reads | no-follow identity/hash then frozen snapshot | symlink, non-file, path replacement, size/hash mismatch, implicit failover rejected |
| Package | prior Core attestation → exact five files, marker last, no-overwrite publish through held `dir_fd` | write/fsync/marker/rename fault, root replacement, existing target, partial staging never complete |
| Recovery | attestation-bound published package reconciles one success | no attestation/self-consistent replacement rejected; deterministic rejection quarantined; unknown/quarantine failure remains recoverable and unhealthy |
| Usage | one occurrence per exact clip; successful job only | preview/fail/cancel/interrupted create zero usage; duplicate replay creates zero extras |
| Ledger | operation/job/receipt then immutable revision/usage | operation/job/revision/occurrence/receipt tamper and bounded rollback fail closed |
| UI | available/in-progress/blocked states are capability-consistent | stale response, post-dispatch unknown, path/nonce/token leak, latest success misread as reusable approval rejected; unknown locks until trusted refresh |
| Compatibility | V1–V7 oracle and legacy behavior remain unchanged | V8 collision/drift fails before domain write; no legacy Timeline/render promotion |

## 8. Rollback and release boundary

- V8 是 additive；回退版本时旧应用必须对 schema 8 fail closed，不得忽略 V8 表继续写旧 schema。
- 能力回退优先关闭 presentation/start 和 workspace capability，保留 V8 job/revision/usage/receipt 可读审计；不删表、不回拨 revision。
- 用户已成功包不是 app cache，回退和 cleanup 不能删除。Staging/quarantine 只能在后续有 scoped cleanup proposal 和可恢复证据时处理。
- 本切片不使 V4 整体或产品变成 release-ready。真实 Electron 视觉/可写验收、Remote CI、signed/clean-machine 与上游完整初剪仍是独立门禁。
