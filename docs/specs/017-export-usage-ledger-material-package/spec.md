# Feature Specification: Export Usage Ledger & Material Packages

- Feature ID：`ML-017`
- 创建日期：2026-08-22
- 状态：`PARTIALLY IMPLEMENTED / ML-017-A+A2+A3 VALIDATED_CONTROLLED_LOCAL / PARENT V4 INCOMPLETE`
- 实施授权：`STAGED / ML-017-A+A2+A3 IMPLEMENTED IN SHARED WORKTREE / PROMOTION BLOCKERS RETAINED`
- 输入决策：[43 问产品对齐记录](../product-decisions-43-questions-2026-08-22.md)
- 当前切片：[ML-017-A Canonical 1080p Export & Success-only Usage Ledger](slices/017-a-canonical-export-usage-ledger/spec.md)；[ML-017-A2 Canonical Usage Projection & Brief Admission](slices/017-a2-usage-projection-admission/spec.md)；[ML-017-A3 Exact Residual Material Identity & Export-Derivative Exclusion](slices/017-a3-exact-residual-derivative-exclusion/spec.md)
- 规范类型：Spec Kit 风格产品规范；具体容器、事务和目录布局留给 `plan.md` / ADR
- 优先级：P0，闭合“做完一条视频以后还知道什么素材能继续用”
- 依赖：[ML-005](../005-video-creative-workbench.md)、[ML-007](../007-local-capability-boundary/spec.md)、[ML-008](../008-unified-media-memory-kernel/spec.md)、[ML-014](../014-agent-navigable-media-wiki/spec.md)、[ML-015](../015-agent-agnostic-creative-protocol/spec.md)

## Overview

每次用户成功导出一条视频，MemoLens 必须同时生成一个可读、可验证的使用记录：用了哪些图片、哪些视频的哪些秒、这些素材在 Timeline 中出现在哪里、对应哪个项目和导出版本。成功导出是使用事实的写入点，不再要求用户多做一次“已发布”确认；预览、失败导出和仅保存项目不计为已用。

默认输出一个本地轻量素材包：

```text
作品目录/
  成片
  脚本
  字幕
  封面（若存在）
  使用清单.txt          # 人可以直接看
  manifest.json         # Agent/Core 可验证
```

父规范保留了字幕、封面、完整素材包和 Wiki 等长期目标。[ML-017-A](slices/017-a-canonical-export-usage-ledger/spec.md) 的实际边界更窄：只导出 canonical Timeline 绑定的 1080p 有意静音硬切成片，probe 必须证明没有 audio stream；包另含 exact Blueprint 脚本、manifest、人类使用清单和 completion marker。[ML-017-A2](slices/017-a2-usage-projection-admission/spec.md) 已新增 Search used/residual projection、strict Renderer adoption 与 same-transaction Brief admission。[ML-017-A3](slices/017-a3-exact-residual-derivative-exclusion/spec.md) 已实现 deterministic exact residual identity、backend/plugin/canonical consumption、same-transaction admission、五表 cold audit 和 successful-output exact-SHA exclusion，当前状态为 `VALIDATED_CONTROLLED_LOCAL`。字幕/封面/转场、source-audio edit/mix、配乐、旁白、混音、final-fidelity render、完整包和 materialized Wiki 仍未实现。B2B4B.1 的 AAC-bearing raw preview transport 不改变 A 的 silent canonical export 合同。

ML-017-A 的提交顺序固定为：发布前 Core durable commit attestation → held output-root `dir_fd` 上 no-overwrite package publication → 与先验 attestation 的物理重验 → successful Export Revision + Usage Occurrences。Package manifest 只定义 package-local 字节/语义；它不替代 Core ledger，也不能在恢复时用一组自洽的新 digest 反向发明 commit intent。

父规范后续轻量包可在受控 relink 设计完成后保存稳定引用、路径提示和 hash；当前 A manifest 保持 path-free，不提供 source locator hint 或 relink workflow，也不复制、不移动原件。用户需要备份或迁移时，未来可显式“生成完整素材包”；系统再把本次实际使用的图片和精确视频片段复制/导出到包内，同时保留它们与原件的 hash、时间范围和 Timeline 映射。完整包也只写用户选择的本地文件夹，与云、自动上传或自动清理无关。

Usage Ledger 反向进入 Media Wiki：导出成片默认不再作为 raw B-roll 候选；原始视频只有确切用过的时间范围被标为 used，未用 remainder 继续可搜索。用户始终可以显式复用已用片段。

## Hypothesis and Falsification

### Hypothesis

把成功导出、精确 source interval、可验证 package 与后续检索连成闭环，能够显著减少重复使用和人工查找，同时避免“一个长视频用过 3 秒就整条作废”的素材浪费，并让项目迁移、复盘和再次编辑可靠得多。

### Falsification / Stop Conditions

1. 无法从已验证 Timeline 确定性地产生 source mapping，必须依赖模型猜测“用了什么”。
2. 使用记录可能在 render 失败、用户取消或文件未落盘时写入 current ledger。
3. 生成任一包会修改、移动、删除、覆盖或自动上传 Library 原件。
4. 区间记录导致长视频未使用 remainder 被错误排除，误排率超过 2%。
5. 轻量包在原件移动但内容未变时不能 relink，或完整包无法验证 copied media 与 manifest 一致。
6. 为了实现账本，导出路径需要用户理解数据库、hash 或内部目录，明显破坏“一次导出即完成”的体验。

## User Scenarios & Testing

### User Story 1：一次成功导出同时得到成片和使用清单（Priority: P1）

用户选择导出目录并完成导出。除了成片，目录中自动有简洁的 `使用清单.txt`；需要自动化时，Agent 读取对应 manifest。用户不需要再回答“是否发布”才能让 MemoLens 记住素材使用情况。

**Independent Test**：对含图片、视频、多次复用、字幕和速度变化的 Timeline 导出，人工核对成片、TXT、manifest 与实际 source reads。

**Acceptance Scenarios**：

1. **Given** 已验证 Timeline，**When** 成片成功写入并通过完整性检查，**Then** export revision 和 usage facts 一次性进入 current，轻量包标记 complete。
2. **Given** render、复制、校验或最终提交任一步失败，**When** 操作结束，**Then** current Usage Ledger 不新增“已用”事实，残留工件不冒充完整包。
3. **Given** 同一视频的 `[10s,14s)` 和 `[40s,45s)` 被使用，**When** 查看清单，**Then** 两段分别可读，不能只显示“该文件已使用”。
4. **Given** 用户导出同一项目的新版本，**When** 完成，**Then** 新旧 export revision 都保留，各自 usage 可追溯，current/supersession 可见。

### User Story 2：检索优先找未用素材，但不会浪费剩余画面（Priority: P1）

下一次创作时，Agent 可以询问“未用过”“尽量新鲜”或“允许复用”。系统按精确 interval 而非文件级标签处理，并默认排除成片 derivative 作为 raw 素材。

**Independent Test**：建立带人工标准的 used/residual 区间，运行未用、允许复用、同文件 remainder、排除成片四类查询。

**Acceptance Scenarios**：

1. **Given** 60 秒原视频仅使用 `[10s,14s)`，**When** 搜索未用素材，**Then** 其他可用范围仍能返回，结果说明 residual 与 used 边界。
2. **Given** 用户说“不要重复上一条”，**When** 有足够候选，**Then** used interval 降权或排除；若没有足够候选，系统诚实返回 gap 并允许用户放宽。
3. **Given** 用户明确允许复用，**When** used span 是最佳候选，**Then** 可以返回并说明曾用于哪个 export，而不是永久隐藏。
4. **Given** 成片被放回 Library，**When** 普通 B-roll 搜索，**Then** 它按 derivative/final role 默认排除，但仍可在作品历史或风格参考中找到。

### User Story 3：轻量包不复制原件，仍能被人和 Agent 理解（Priority: P1）

父规范的后续 relink slice 目标是让默认包体积接近成片本身，并让 `使用清单.txt` 使用用户可读名称/时间范围、`manifest.json` 使用稳定 identity、hash 和受控 locator hint。当前 A 的 manifest/TXT 均不承诺 source locator hint 或 relink；它只提供 path-free identity、source mapping、物理 digest 与自动化所需的 package-local 证明。

**Independent Test**：在原 Library 在线、移动但相同、离线和内容被替换四种状态打开轻量包并运行验证。

**Acceptance Scenarios**：

1. **Given** 原件仍在原位置，**When** 验证包，**Then** 所有引用解析、hash 与 source interval 一致。
2. **Given** 原件移动但内容 hash 相同，**When** 用户重新连接 Library，**Then** relink 形成显式 operation，不改写旧 manifest。
3. **Given** 原件暂时离线，**When** 打开包，**Then** 成片、脚本、字幕、清单和历史仍可读；需要原件的再编辑明确受限。
4. **Given** 原路径内容已替换，**When** 验证，**Then** 返回 identity mismatch，绝不把新字节当作旧来源。

### User Story 4：按需生成本地完整素材包（Priority: P1）

用户需要迁移、备份或交给另一个工具时，点击“生成完整素材包”并选择本地目录。包中加入实际使用图片和精确视频片段，不复制整座素材库，也不改变原件。

**Independent Test**：由轻量 export 生成完整包，在没有原 Library 的干净环境验证、播放素材片段并检查 Timeline 映射。

**Acceptance Scenarios**：

1. **Given** 完整包目标、export revision 和预计体积已确认，**When** 生成成功，**Then** used media、manifest、成片和项目 sidecars 齐全且 hash 可验证。
2. **Given** 一个原视频只使用多个短区间，**When** 打包，**Then** 包含对应可识别片段及 source mapping，不默认复制整条原视频。
3. **Given** 目标磁盘空间不足或复制中断，**When** 失败，**Then** 原件不变，旧轻量包不变，未完成目录不标为 complete 且可安全重试/清理。
4. **Given** 用户对同一 export 重试，**When** 使用相同 idempotency identity，**Then** 不产生重复 usage facts 或不可区分的包。

### User Story 5：使用记录可审计、可修正但不伪造历史（Priority: P1）

如果发现旧 manifest 错误、原件 relink 或 Timeline 映射需要修复，系统新增 correction/supersession；不直接编辑掉原清单。用户也可以把某个导出标记为测试版，但成功导出已经发生这一事实仍保留。

**Independent Test**：注入 manifest 错误、relink、导出误分类和 validator 升级，检查 history、current resolution 和 downstream search。

**Acceptance Scenarios**：

1. **Given** 旧 usage 记录有 bug，**When** 修复工具确认，**Then** 新 correction 指向原 revision 并说明原因，历史字节与旧项目 binding 保留。
2. **Given** 用户标记导出为测试版，**When** 下次搜索，**Then**检索策略可区别 test/final，但不得声称该导出从未使用素材。
3. **Given** validator 升级发现旧包不完整，**When** 打开，**Then** 状态变为 warning/stale，不自动删除或重写用户文件。

## Edge Cases

- 同一 source span 在一条 Timeline 中重复出现或以不同速度、方向、裁切和音量出现。
- 转场、speed ramp、freeze frame 或光流插值使 timeline time 与 source time 非线性对应。
- 图片在 Timeline 中持续若干秒，但 source 没有时间区间。
- 一个 compound/nested sequence 引用另一个项目或预渲染片段。
- 代理媒体和原件同时存在；render 实际从代理读但 provenance 应回到原件。
- 视频容器时间基、可变帧率、旋转 metadata 或关键帧寻址造成边界误差。
- 输出与某个 Library 文件 hash 相同，或用户把成片改名后重新导入。
- 目标文件系统不支持可靠目录原子 rename、大小写敏感度不同或 Unicode normalization 不同。
- 脚本、字幕或封面在导出时不存在；某 sidecar 是用户明确跳过而非错误。
- 同一项目同时发起两个导出；一个成功、一个失败。
- 用户覆盖已有同名包、取消覆盖、或确认后目标内容发生变化。
- 完整包含有第三方音乐、字体或媒体，但迁移许可不同于项目本地使用许可。

## Requirements

### Functional Requirements

#### Export transaction 与 usage 真值

- **FR-001**：只有用户发起的 export 在成片与规定 sidecars 成功写入并通过验证后，才创建 successful Export Revision 与 current Usage Facts；preview/cache/save project 不计为 used。
- **FR-002**：导出成功本身是“素材已用于一个输出版本”的事实写入点，不额外要求发布确认；发布状态可以是独立可选 metadata，不能影响已发生 usage。
- **FR-003**：Export Revision 必须固定 project、Blueprint、Timeline、Wiki/analysis、render profile、source mapping、output digest、tool/runtime manifest 与完成时间。
- **FR-004**：失败、取消、超时、磁盘满、校验失败或部分写入不得产生 successful usage；错误和 staging 状态必须可区分、可恢复和可清理。
- **FR-005**：同一操作重试必须幂等；同一 export revision 不得因响应丢失产生重复 usage rows、包或历史。
- **FR-006**：重复导出、新版本导出和不同平台变体必须是可区分 revision，支持 supersedes/derived_from，但不覆盖旧记录。

#### 精确 source mapping

- **FR-007**：每个 Timeline media occurrence 必须映射到稳定 source asset；视频至少记录规范化半开区间 `[start_ms,end_ms)`，图片记录 asset identity 与 Timeline occurrence。
- **FR-008**：清单必须保留每次 occurrence 与合并后的 source interval 两种视图：前者用于重放，后者用于 used/residual 计算。
- **FR-009**：速度变化、reverse、freeze、transition handle、nested sequence 与代理媒体的 mapping 语义必须明确；当前版本无法精确表达时 export validation 必须阻止错误记录或标记 unsupported，不能猜测。
- **FR-010**：Used interval 由验证过的 Timeline/source mapping 确定性产生，不得由模型、文件名或成片视觉反推。
- **FR-011**：Residual interval 必须由可用 source domain 减去规范化 used union 得到；短 padding、技术损坏区间和用户排除是独立约束，不能混成 used。
- **FR-012**：source replacement、relink 与 path move 不改变稳定 asset identity；hash 不匹配时不得解析为原来源。

#### 轻量素材包

- **FR-013**：每次 successful export 必须创建一个与该 revision 对应的新本地轻量包，不更新、合并或覆盖旧包；其中至少包含成片、`使用清单.txt`、machine-readable manifest，以及当前切片所支持的 sidecars。缺少可选 sidecar 必须在 manifest 明示。
- **FR-014**：`使用清单.txt` 必须用人可读名称说明项目/版本、导出时间、每个图片、每个视频时间段、Timeline 位置、复用状态与原件不被移动的事实；不要求用户理解内部 ID。
- **FR-015**：manifest 必须包含 schema version、package/export/project identity、content digests、asset/span refs、source mapping、file roles、relative package paths、受控 source locator hints、completion state 与 validation result。
- **FR-016**：轻量包默认不得复制 Library 原媒体；其引用应允许 source 在线时验证、移动后 relink、离线时诚实降级。
- **FR-017**：包内容必须可用普通文本/JSON 工具理解关键逻辑；App 缓存、数据库或聊天记录不得成为唯一解释来源。

#### 完整素材包

- **FR-018**：完整包只在用户显式选择准确 export revision、目标目录、覆盖策略并确认预计体积后生成；不得自动上传或写入未授权目录。
- **FR-019**：完整包必须加入本次实际使用的图片和精确视频片段及其 manifest mapping；除非用户另选备份模式，不默认复制未用素材或整个原视频。
- **FR-020**：从原视频导出的 used fragment 必须声明它是 package derivative，并保留原 asset hash、source interval、转码/stream-copy 方法和自身 digest；不得冒充原文件字节。
- **FR-021**：完整包生成必须不修改、不移动、不删除、不覆盖 Library 原件；失败不得改变轻量包或 Usage Ledger。
- **FR-022**：完整包 validator 必须在没有原 Library 时检查 package files、digests、required roles、source mapping 和 schema；可移植范围必须明确，不声称包含未复制的余量素材。
- **FR-023**：第三方媒体、字体、音乐、LUT 或模板若不具备可打包许可，必须排除或标记 required_external，不能因本地项目可用就自动复制。

#### 检索反馈与作品角色

- **FR-024**：成功 usage 必须投影到 Media Wiki/Search，支持 `unused_only`、`prefer_unused`、`allow_reuse`、`used_in` 与 `residual_of` 的语义等价查询。
- **FR-025**：文件级“已用”不得永久排除视频的未用 interval；检索结果必须能解释具体已用范围与可用 remainder。
- **FR-026**：successful export output 必须具有 derivative/final/test 等可修正角色；默认 raw素材检索排除 derivative output，但作品历史和风格参考保留。
- **FR-027**：将成片重新放入 Library、改名或复制路径不得使其失去已知 derivative identity；无法确定时返回 candidate duplicate/unknown，不错误断言。
- **FR-028**：用户可以显式复用 used span；系统不得把“优先未用”升级成不可绕过的禁令。

#### 修正、隐私与可恢复性

- **FR-029**：Usage、Export 和 Package 状态的修正必须通过新 revision/correction/supersession 表达，旧 manifest 与历史项目引用保持可解释。
- **FR-030**：导出、覆盖、完整包和清理 staging 必须使用 ML-007/015 定义的准确 scoped capability；Agent 不能构造任意目标路径或跳过确认。
- **FR-031**：清单与 manifest 对外分享前必须允许用户检查路径/名称等隐私字段；内部绝对路径不是跨设备可移植 identity，也不应成为非必要默认展示。
- **FR-032**：系统必须提供 package verify、source verify/relink、usage inspect 和 incomplete cleanup proposal；cleanup 不能扩大为自动删除 Library 原件。
- **FR-033**：任何 package 标记 complete 前必须验证 required files、digests、manifest 自身一致性，以及发布前 durable Core job/operation commit attestation binding；successful Export Revision 只能在 package 完成并按该 attestation 物理重验后创建，不能成为 completion marker 的循环前置。
- **FR-034**：未来若增加“归档建议”，只能消费 Usage Ledger 生成 proposal；不得由本规范推导自动移动、删除或上传原件。

### Key Entities

- **Export Revision**：只为已经完成并验证的成功作品创建，固定项目/Timeline/render/source inputs 与输出 digest；失败、取消、中断和未知结果属于 operational Export Job/attempt，不创建失败 revision。
- **Source Mapping**：Timeline occurrence 到稳定 asset/span 的确定性映射。
- **Usage Occurrence**：素材在某 export Timeline 中的一次实际出现。
- **Usage Interval**：同一视频在该 export 或历史 exports 中规范化后的已用区间。
- **Residual Interval**：原视频可用 domain 中未被 usage union 覆盖的范围。
- **Lightweight Material Package**：成片、sidecars、可读清单和引用 manifest，不含 Library 原媒体副本。
- **Full Material Package**：在轻量包基础上加入实际使用图片/视频片段的本地可验证包。
- **Package Derivative**：从原件精确范围生成的包内媒体，保留 provenance 但不是原件字节。
- **Package Manifest**：包身份、文件角色、digest、source mapping、许可和完成状态的 package-local 机器真值；Core ledger/commit attestation 才是 Export/Usage admission 与恢复的权威。
- **Human Usage List**：面向用户的 TXT 投影，不替代 manifest。
- **Derivative Role**：final、test export、preview、proxy、package fragment 等非 raw 素材身份。
- **Usage Correction**：不删除历史的修正或 supersession。

## Success Criteria

### Measurable Outcomes

- **SC-001**：对所有支持的 Timeline operation fixtures，successful export 的 occurrence/source mapping 与实际 renderer reads 一致率 100%；模型参与 usage 判断次数为 0。
- **SC-002**：在至少 500 个含重叠、重复、速度变化和多段复用的标注区间上，used union 与 residual 计算准确率 100%；未使用 remainder 被文件级错误排除的次数为 0。
- **SC-003**：故障注入覆盖 render、sidecar、manifest、copy、digest、rename/commit 每个阶段；失败后 successful Usage Ledger 误写为 0，原件修改为 0，旧完整包损坏为 0。
- **SC-004**：100% successful export 有可读 TXT、manifest、成片 digest 与固定 project/Timeline revision；缺少可选 sidecar 时 manifest 说明率 100%。
- **SC-005**：原路径移动但内容相同时，用户重新连接后引用 relink 成功率 100%；source replacement 被误接受次数为 0。
- **SC-006**：完整包在无原 Library 的干净环境验证 required files/digests/source mapping 成功率 100%；未经许可资产被复制次数为 0。
- **SC-007**：未获 scoped confirmation 的 export、overwrite、full package 与 staging cleanup 成功数为 0；任何包自动上传次数为 0。
- **SC-008**：在 used/residual 检索任务上，已用区间规避准确率 ≥95%，且可用 remainder 召回相对不含 usage 的时间检索 baseline 下降不超过 2 个百分点。
- **SC-009**：相对人工维护“用过素材.txt”，用户导出后额外录入动作数为 0，且至少 90% 目标用户能在 30 秒内从 TXT 找到某素材用过的时间范围。
- **SC-010**：同一 idempotency identity 重试 100 次，successful export/usage/package identity 均不重复；不同 revision 可清楚区分。

## Validation Matrix

最少覆盖：

| 维度 | 必测组合 |
| --- | --- |
| 素材 | 图片、恒定帧率视频、可变帧率视频、代理、同源多路径 |
| Timeline | 重复使用、重叠、裁切、速度变化、freeze、transition handle、nested/unsupported |
| 导出 | 首次、重试、新版本、平台变体、并发、取消、失败 |
| 文件系统 | 正常、空间不足、只读、目标变化、Unicode、大小写差异 |
| 来源状态 | 在线、移动且 hash 相同、离线、内容替换 |
| 包 | 轻量、完整、缺少可选 sidecar、第三方资产不可复制、manifest 损坏 |

不支持的 Timeline 映射必须在 export 前给出稳定 unsupported，不得为了通过测试生成不准确 usage。

## Rollback and Degradation

- 先从已支持的基础 clip/image operation 生成 usage；复杂映射未定义时禁止把结果标为精确。
- successful Export Revision 与 Usage Occurrences 在 Core 中原子提交；不会出现“成功 revision 已存在但 core usage 尚待补交”。后续 Wiki/Search projection 失败时，只把独立 downstream projection job 标为 pending-reconciliation；成片与成功 ledger 不丢失，检索也不得假装 projection 已更新。
- 完整包失败不影响轻量包、成片、项目和 usage；重试复用固定 export revision。
- TXT 可从 manifest 重建；TXT 损坏不改变 machine truth。Manifest 不可验证时整个包不得标 complete。
- 新 schema 无法读取旧包时保持只读并输出 migration proposal，不原地覆盖。

## Out of Scope

- 不判断用户是否真的上传或发布到社交平台。
- 不自动上传网盘、云盘、社交平台或远端备份。
- 不自动移动、删除或释放原素材空间。
- 不把完整素材包扩展为整个 Library 的备份工具。
- 不在本规范定义项目协同传输、版权清关服务或社会化发布分析。
- 不要求复制完整原视频；默认只包含实际使用片段及原件引用。

## Assumptions

- ML-005/015 最终提供经过验证、可重放的 Timeline 和 typed operations。
- ML-008 提供稳定 asset/source identity、content digest 与 revision。
- ML-014 能消费 usage/residual/derivative projection，但不成为 Usage 真源。
- 原始文件可能移动或暂时离线，因此 path 只是 locator，不是 identity。
- 用户最新决策以成功 export 记录 usage；publication status 不是必要确认点。

## Authorization Boundary

2026-08-23 的 staged implementation authorization 已覆盖 [ML-017-A](slices/017-a-canonical-export-usage-ledger/spec.md) 所定义的 exact native export、独立 export ledger、五角色轻量包与 success-only Usage Occurrences，[ML-017-A2](slices/017-a2-usage-projection-admission/spec.md) 的 used/residual Search、strict Renderer adoption、same-transaction Brief admission 与 bounded polling，以及 [ML-017-A3](slices/017-a3-exact-residual-derivative-exclusion/implementation-evidence.md) 的 exact residual/derivative 端到端消费与 durable V19 output-root anchor。A 已观察到真实 Electron native directory→successful package；该运行早于 A2/V9，因此 fresh polling/admission/V9 native journey 仍未验证。A3 已通过 focused、production oracle、Codex cache parity、DeepSeek fresh loader 与第一轮整仓本地门禁，但未运行 Codex/DeepSeek 真实 model call、双向 host-model-UI T053/T054、Remote CI 或 release。父规范仍只能写成 partially implemented，不得写成 V4 complete。

父规范其余范围不因 A/A2/A3 而自动获得实现声明：字幕/封面/转场、source-audio edit/mix 与 stream/content/sync attestation、配乐、旁白、混音、final-fidelity render、ML-017-B 完整素材包、source locator hint/relink、Usage correction/supersession、materialized Wiki、发布/云连接和任何自动移动/删除/归档原件仍未实现或未验证。任意输出路径仍只能由 Electron main 在当次原生交互中一次性批准；A 的 silent master 与 B2B4B.1 的“AAC 可在 raw preview transport 中存在但页面禁止音频播放”是两条不同合同，都不证明音频剪辑/混音已交付。
