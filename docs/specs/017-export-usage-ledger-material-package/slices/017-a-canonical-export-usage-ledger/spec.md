# Feature Specification: Canonical 1080p Export & Success-only Usage Ledger

- Feature ID：`ML-017-A`
- 创建日期：2026-08-23
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCAL VALIDATION WITH RESIDUALS`
- 授权依据：用户于 2026-08-23 授权按唯一内容真源链持续实施必要切片
- 父规范：[ML-017](../../spec.md)
- 前置：[ML-015-B2B deterministic Timeline lowering](../../../015-agent-agnostic-creative-protocol/slices/015-b2b-deterministic-timeline-lowering/spec.md)
- 后续：[ML-017-A2 Canonical Usage Projection & Brief Admission](../017-a2-usage-projection-admission/spec.md) 已实现 occurrences→used/residual search 与 Brief admission；不改变本切片的 success-only writer
- 实施证据：[implementation-evidence.md](implementation-evidence.md)

## Why this slice exists

MemoLens 已有独立的 canonical Blueprint、Coverage Plan 和 Timeline，但 legacy `timelines` / `render_jobs` 仍是另一条历史路径，而预览和旧的 “Verified Save As” 也不能证明用户完成了最终导出。如果直接解锁 legacy export，会让 Timeline、文件系统结果和 Usage 出现多个互相漂移的真源。

本切片只闭合一条窄而可审计的路径：

```text
exact current Blueprint + Coverage + Canonical Timeline
  → path-free exact export presentation
  → Electron main 原生目录选择 + held device/inode identity + one-shot approval
  → fixed-source 1080p silent hard-cut render
  → durable Core commit attestation + job commit_pending
  → five-role lightweight package
  → verified filesystem completion against the prior attestation
  → immutable successful Export Revision + Usage Occurrences
```

Export Revision 只记录已经成功的作品事实；运行中、失败、取消和中断由独立 operational job 表达，不伪造 Usage。

## User stories

### US1：用户审批的是屏幕上那个精确版本（P1）

用户在 canonical Blueprint workspace 点击导出后，Desktop main 先获取一个无路径 presentation，展示精确 Timeline binding、时长、比例、clip 数、固定 profile 和包角色。只有原生目录选择完成后，main 才为这一次手势生成 nonce 并提交命令。main 必须以 `O_NOFOLLOW` 打开所选目录、固定 device/inode，并在 Core 完成身份比较前持有 descriptor；选择结果不能退化为一条可被替换后重开的路径字符串。

**Independent test**：在 presentation 发出后分别修改 Timeline、Coverage、Blueprint、source head 或重启 backend，验证旧 presentation 不能被当作新状态的授权。

**Acceptance scenarios**：

1. **Given** canonical Timeline 及其 Blueprint/Coverage/source bindings 全部 current，**When** main 请求 presentation，**Then** 返回的对象固定 exact digests 且不包含绝对路径、credential 或 nonce。
2. **Given** 用户取消原生目录选择，或命令在 receipt/nonce/presentation admission 前被拒绝，**When** approval flow 结束，**Then** 不创建 export root locator、operation、job 或 receipt，也不遗留 prepared descriptor。
3. **Given** presentation 后任一 exact binding 变化，**When** main 提交旧 presentation，**Then** Core fail closed，不使用新 head 替换用户看过的版本。
4. **Given** renderer/Agent 试图直接提交目标路径，**When** 调用非 main surface，**Then** 不获得 native credential、nonce 或导出写权。
5. **Given** 原生选择后目录 inode 被替换，**When** backend 准备或执行命令，**Then** 在零领域/locator 写入下拒绝；worker 只沿 Core-held `dir_fd` 写入已批准目录。
6. **Given** 命令可能已发出但响应丢失、返回 5xx 或响应身份无法验证，**When** main 无法证明结果，**Then** renderer 显示 `unknown` 并锁定再次导出，直到一次可信 canonical workspace refresh 重新分类结果；不得把未知写成失败并直接重试。若可信 workspace 返回 latest job=`interrupted`，状态必须保持 `blocked / export_recovery_required`，用户需重启 MemoLens 触发 activation recovery，refresh 本身不能把它变回 available。

### US2：首个最终输出是可重放的 1080p 静音硬切（P1）

本切片将 exact canonical Timeline 机械地降为单一 render plan。它不重新选材、不加转场、不合成字幕、不混音，也不用 legacy Timeline 作 fallback。

**Independent test**：对图片、视频及四种支持比例的 canonical Timeline 执行导出，核对每个 clip 的 Timeline 时间、视频 source 半开区间、frozen source digest 和 final master probe。

**Acceptance scenarios**：

1. **Given** `16:9` / `9:16` / `1:1` / `4:5` Timeline，**When** 导出，**Then** 分辨率分别是 `1920×1080` / `1080×1920` / `1080×1080` / `1080×1350`，固定 30 fps。
2. **Given** canonical clip 仅允许 `cover`、hard cut 和 source audio disabled，**When** render，**Then** master 为静音硬切；任何超出这个 contract 的能力不被猜测降级。
3. **Given** source locator 指向符号链接、非普通文件、大小/hash 不匹配或已变更 source，**When** renderer 准备读取，**Then** 在 FFmpeg 前拒绝，不用同 asset 的其他 source 暗中替换。
4. **Given** source 验证成功，**When** FFmpeg 开始，**Then** 它读取 app-owned frozen snapshot，runtime/actual-read manifest 固定实际读取证明而不泄露路径。

### US3：轻量包只在完整且未覆盖时对用户可见（P1）

默认包是固定的五角色集合：

```text
<package_basename>/
  video.mp4
  script.txt
  manifest.json
  使用清单.txt
  .memolens-complete.json
```

`script.txt` 是 exact Blueprint script blocks 的确定性投影，不是新的可写脚本真源。`manifest.json` 内联 source mapping、Usage Occurrences、runtime/actual-read proof 和所有 required digest。首版不包含字幕或封面，manifest 必须将二者标记为 absent。

**Independent test**：在 pre-attestation、render、script、manifest、usage list、marker、fsync、rename、post-publish 和 DB completion 各阶段注入故障，并对同名目标、无 attestation、自洽替换包、Core-success wrapper failure、unknown、quarantine failure、中断后重启和已发布但 DB 未完成的状态做验证。

**Acceptance scenarios**：

1. **Given** 所有四个内容文件已写入、fsync 并通过 digest 验证，**When** staging 进入完成阶段，**Then** completion marker 最后写入，再以 no-overwrite rename 发布整个目录。
2. **Given** 目标已存在，**When** 导出，**Then** 不覆盖、不合并、不删除旧包。
3. **Given** Core 已在发布前持久化 exact commit attestation 且 job 为 `commit_pending`，**When** 包发布后 app 恢复，**Then** 只有物理包与这份先验 attestation、job、Timeline 和全部 digests exact-equal 时才补交成功 revision；包内一组自洽的新 digest 不能替代 Core intent。
4. **Given** 发布后 Core 给出可证明的 deterministic rejection，**When** recovery 仍能用 exact attestation 识别该包，**Then** 才将包移入确定性 quarantine，不扩大为原素材删除。
5. **Given** Core success 已持久化，**When** wrapper/transport 随后报错，**Then** successful ledger 保持权威，不能 quarantine 已成功包。
6. **Given** DB 结果未知、可信 job 读取失败或 quarantine 失败，**When** recovery 无法安全分类，**Then** 保留包和 durable commit intent，job 保持 recoverable，runner/activation fail closed；不得伪造 failed、success 或健康状态。

### US4：Usage 只由成功导出产生（P1）

Usage Occurrence 从 exact Timeline/source bindings 确定性派生，不从文件名、成片视觉或模型推测。每个 clip 是一次 occurrence；视频保留 source 半开区间，图片仅保留 asset/source identity 与 Timeline 位置。

**Independent test**：对成功、render 失败、打包失败、取消、崩溃恢复和同一 idempotency key 重放，核对 Export Revision 和 Usage row 的数量与字节。

**Acceptance scenarios**：

1. **Given** 发布前 commit attestation 已持久化，且成片、脚本、manifest、清单、marker 均已按该 attestation 验证，**When** Core 完成 `commit_pending` job，**Then** successful Export Revision 和全部 Usage Occurrences 在同一 DB 事务中写入，job 才进入 `succeeded`。
2. **Given** preview、仅保存项目、失败、取消或未完成 job，**When** 查看 Usage ledger，**Then** 没有对应 successful revision/occurrence。
3. **Given** 同一命令因响应丢失重试，**When** idempotency key 与 request digest 一致，**Then** 返回同一永久 receipt/job；同 key 不同 request 拒绝。
4. **Given** 同一 source span 在 Timeline 出现多次，**When** 导出成功，**Then** 保留每次 occurrence；used union 与 residual 为从 immutable occurrences 导出的 projection，不是可写第二真源。

## Functional requirements

### Exact authority and admission

- **FR-A-001**：只有 Electron main-owned native user gesture 可创建 canonical export；renderer、MCP、Agent、legacy browser fallback 不得获得目标路径写权或可重用授权。
- **FR-A-002**：main 必须先获取 path-free exact presentation，再在同一原生交互中选择目录并一次性批准；main 必须固定选择时的 device/inode 并持有 no-follow descriptor，Core 必须在创建 job 前比较 exact directory identity。
- **FR-A-003**：presentation 必须固定 project、Timeline revision/digest/source-binding digest、上游 Blueprint/Coverage bindings、profile、比例、时长、clip 数和五个 package roles。
- **FR-A-004**：Core 在创建 job 前必须重验当前 Blueprint、Coverage、Coverage evidence、Timeline 和 fixed source；任一不一致都 fail closed。
- **FR-A-005**：native nonce 只能消费一次；永久 idempotency receipt 只能重放同一 canonical request。user-export root 在 admission 前只能是进程内 prepared descriptor；root locator、operation、job 与 receipt 必须在同一命令事务物化，拒绝或 replay 不得遗留 locator/descriptor。
- **FR-A-006**：所有 renderer-safe presentation/job/workspace 投影必须排除绝对路径、root locator、permission fingerprint、native nonce、token 和 credential。

### Fixed render contract

- **FR-A-010**：首版 profile 唯一值为 `export-1080p`，固定 30 fps、`cover`、静音、hard cut，且只支持 `16:9` / `9:16` / `1:1` / `4:5`。
- **FR-A-011**：render 必须使用 exact canonical Timeline 和独立 source-bindings manifest；不读 legacy `timelines`，不写 legacy `render_jobs`。
- **FR-A-012**：每个 source 必须以 no-follow 语义打开、验证 identity/size/hash，再复制到 app-owned frozen snapshot；FFmpeg 不得重新按原 locator 打开素材。
- **FR-A-013**：runtime manifest 与 actual-read manifest 必须固定 renderer/profile/output geometry/source snapshot proofs，并保持 path-free。
- **FR-A-014**：未支持的转场、speed/reverse/freeze/nested、字幕、封面、任何音轨能力（source audio、配乐、旁白、混音）或其他 Timeline capability 必须拒绝或在上游保持 unavailable，不得猜测转换；A 只接受有意 silent/no-audio-stream contract。

### Lightweight package and filesystem commit

- **FR-A-020**：轻量包固定为 `video.mp4`、`script.txt`、`manifest.json`、`使用清单.txt`、`.memolens-complete.json` 五个 required roles；字幕和封面明确 absent。
- **FR-A-021**：`script.txt` 必须由 Timeline 固定的 exact Blueprint script blocks 确定性投影，并以 digest 绑定到 package/export revision。
- **FR-A-022**：`manifest.json` 是 package-local 字节与语义真值，必须固定 export/job/project/Timeline/profile、Usage Occurrences、runtime/actual reads、source policy、roles 和 video/script digests；它不替代 Core 的 Export/Usage 真值。`使用清单.txt` 只是人类投影。Completion marker 额外绑定 video/script/manifest/usage 文件 digests；发布前的 Core commit attestation 固定预期 manifest/usage/runtime/physical digests，成功 Export Revision 再引用同一证明，避免自引用 digest 循环和恢复时的 TOFU。
- **FR-A-023**：轻量包不复制 Library 原图片/原视频，不移动、删除、覆盖或上传原件。
- **FR-A-024**：所有内容必须沿 Core-held output-root `dir_fd` 先写 job-owned staging 并 fsync，completion marker 最后写入；发布必须使用 no-overwrite 目录提交，不按 locator 重开或更新旧包。
- **FR-A-025**：Core 必须在文件系统发布前原子持久化 immutable commit attestation 并将 job 置为 `commit_pending`；随后才允许按 attested manifest 发布和物理重验。重启只能用先验 attestation 补交 exact 包；deterministic rejection 才允许 quarantine，未知结果保持 recoverable/unhealthy 并阻断激活或新批准。

### V8 ledger and success-only usage

- **FR-A-030**：V8 必须使用独立 additive ledger：immutable export operations、operational export jobs、success-only immutable export revisions、immutable usage occurrences 和 permanent receipts。
- **FR-A-031**：operational job 只允许 closed state machine 中的转移，终态行不可改写；app 激活时将未完成 job 标为 interrupted 并尝试有界恢复。任何 terminal write、可信读取或 quarantine 未决状态都必须使 runner unhealthy；activation recovery 未闭合时不得暴露新 runtime。
- **FR-A-032**：只有 durable commit attestation 存在，且 exact package 完整性与该 attestation 相符后，才能在同一事务写 successful Export Revision、全部 Usage Occurrences 并将 job 设为 `succeeded`。
- **FR-A-033**：失败、取消、中断、preview 和 legacy Save As 产生的 successful Export Revision/Usage Occurrence 数必须为 0。
- **FR-A-034**：每个 Usage Occurrence 必须固定 clip、asset/source identity、Timeline 区间、source-binding digest；video 额外固定精确半开 source 区间。
- **FR-A-035**：used union 和 residual 只能从 immutable Usage Occurrences 和 source domain 导出，本切片不创建可写 `used` 标签或文件级布尔真源。
- **FR-A-036**：V8 不修改、提升或 fallback 到 legacy `timelines`、`render_jobs` 和 legacy export grant。
- **FR-A-037**：post-dispatch 响应丢失、5xx、malformed 或 response identity mismatch 必须投影为 `unknown`，renderer 在可信 workspace refresh 前不得再次发起导出；未知结果不得自动重放为新命令。可信 refresh 若发现 latest job 为 `interrupted`，workspace 必须投影 `blocked / export_recovery_required / capability=false`，并提示重启 MemoLens 运行 activation recovery，而不是仅靠 refresh 重新开放。

## Canonical and operational entities

- **Canonical Export Presentation**：原生批准前展示的 path-free exact revision 摘要。
- **Export Operation**：原生一次性授权所产生的 immutable command record。
- **Export Job**：render/package/commit 的可恢复 operational outbox；不是成功作品事实。
- **Commit Attestation**：发布前持久化在 Core job 中的 immutable、path-free commit intent；固定预期 package manifest、Usage/runtime/physical digests，是 recovery 的先验权威。
- **Export Revision**：只在成功后创建，固定上游内容、实际读取、输出和包 digests。
- **Usage Occurrence**：一个 Timeline clip 在一个 successful Export Revision 中的确定性素材使用事实。
- **Permanent Receipt**：绑定 database/principal/project/command/idempotency key/request/result 的永久重放记录。
- **Lightweight Package Manifest**：用户目录中的 machine-readable package-local 真值；不替代 Core ledger 或 commit attestation。

## Out of scope and residual boundaries

- 字幕生成/烧录、封面生成、转场、speed/reverse/freeze/nested sequence，以及任何音轨能力（source audio、配乐、旁白、混音）；A 的 master 有意为 silent，probe 必须证明不存在 audio stream。
- ML-017-B 完整素材包、使用片段复制/转码、无 Library 再编辑、权利/许可打包。
- Usage correction/supersession、导出角色（final/test）修正、relink 工作流。
- Media Wiki/Search 的 `unused_only` / `prefer_unused` / `allow_reuse` 投影与用户可见 residual explorer。
- 云同步、社交发布、公开链接、自动上传、自动移动/删除/归档原件。
- 用 legacy preview、legacy render job 或 Electron “Verified Save As” 冒充 final export。
- 真实 Electron 视觉/可写路径验收、Remote CI、签名包、clean-machine 与 release admission；这些证据未由本文件推导。

## Success criteria

- **SC-A-001**：未经 Electron main 原生一次性批准的 canonical export 成功数为 0；renderer 投影中路径/nonce/credential 泄漏为 0。
- **SC-A-002**：支持的 Timeline fixtures 中，render plan、actual reads 与 Usage Occurrences 对 exact clip/source mapping 的一致率为 100%。
- **SC-A-003**：四种支持比例全部输出约定的 1080p geometry、30 fps、silent、hard-cut；未支持能力被误接受数为 0。
- **SC-A-004**：成功包的五个 required roles/digests/marker 一致率为 100%；同名旧目录覆盖数为 0。
- **SC-A-005**：对 render/package/publish/DB completion 故障和取消注入，错误 successful Export Revision/Usage Occurrence 写入为 0。
- **SC-A-006**：`commit_pending` 恢复只补交与发布前 durable attestation exact-equal 的 verified package；自洽替换包、缺失 attestation 和未知结果均不能创建 revision/usage，重放不重复创建 revision/usage/receipt。
- **SC-A-007**：V1–V7 migration/checksum 保持不变；fresh V8、V7→V8、collision、tamper、rollback 和中断恢复门禁通过后，才可将验证状态升级。
- **SC-A-008**：focused Core/artifact/service/API/Electron/renderer tests、一次真实 FFmpeg 小包检查与 final-diff `npm run check` 均记录 exact environment/count/exit status 后，才能声称 local validation；Remote/Desktop 视觉/release 仍需独立证据。

## Promotion rule

当前代码、命令与真实包工件已在 [implementation-evidence.md](implementation-evidence.md) 对齐，状态升级为 `LOCAL VALIDATION WITH RESIDUALS`；这仍不等同于已验证产品闭环。真实 Electron native directory→success package 已观察，但 A2 polling/Usage admission 后尚未 fresh 复验；unknown/restart、Remote CI、release 与范围外能力保持未验证，V4 的整体产品结果仍受真实可播放/编辑上游链和用户可见验收约束。
