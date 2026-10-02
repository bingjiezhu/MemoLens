# Feature Specification: Evidence-backed Retrieval & Privacy Benchmark

- Feature ID：`ML-004`
- 创建日期：2026-08-20
- 状态：`PROPOSED`
- 实施授权：`NONE`
- 组合决策：[MUST-NOW / 004-A Lite](../implementation-decisions-2026-08-20.md)；先做黄金旅程、隐私与当前 baseline，不阻塞已知安全修复
- 规范类型：Spec Kit 风格的产品—架构混合规范
- 证据截止：2026-08-20
- 优先级：P0，所有结果型宣传与 Spec 005/006 发布声明的前置门槛
- 研究依据：[前沿调研](../../frontier-research-2026-08-20.md)

## Overview

MemoLens 需要一套可复现、隐私安全、能阻止质量倒退的评测系统。它必须覆盖私人照片检索、视频时间定位、Creator Memory 更新、故事证据、性能和 local-first 网络边界，并保存足够工件让另一位维护者复跑。

本规范补齐 Spec 005 和 006 已依赖、但此前仓库中不存在的 Spec 004。它不追认任何已有性能或隐私宣传。只有新基线按本规范冻结后，结果型声明才有效。

## Hypothesis and Falsification

### Hypothesis

如果 MemoLens 对查询、相关结果、无答案、时间范围、用户修正、证据引用和网络行为建立冻结基线，那么后续架构与模型变化可以用同一门槛判断是否真正提升，而不是依赖 demo 印象。

### Falsification

评测系统若不能由第二位维护者在冻结环境中复现，或无法区分检索、融合、生成和隐私失败，则不具备发布门槛资格。任何新能力在固定种子重复实验中低于预注册门槛时，必须回退或继续保持实验状态。

## User Scenarios & Testing

### User Story 1：维护者阻止质量回归（Priority: P1）

维护者准备合并新的检索、索引、记忆或创作逻辑时，可以在同一数据、同一查询和同一环境上比较当前版本与候选版本，并看到具体失败类别。

**Why this priority**：没有冻结基线，后续所有“跃升”都无法证实，且可能用个别 demo 掩盖普通查询回归。

**Independent Test**：仅使用合成或许可数据运行候选版本，生成机器可读结果和人类可读差异报告，不需要修改真实用户库。

**Acceptance Scenarios**：

1. **Given** 已冻结的 benchmark manifest 和 baseline，**When** 运行候选版本，**Then** 报告按任务、语言、媒体类型、来源数量和难度展示差异。
2. **Given** 候选版本总平均分上升但三来源查询明显下降，**When** 生成结论，**Then** 报告必须把该退化列为失败，不能只显示总平均。
3. **Given** 模型、预处理、数据或运行环境变化，**When** 结果被比较，**Then** 报告必须标记不可直接比较或建立新基线。

### User Story 2：用户获得可验证的 local-first 承诺（Priority: P1）

用户选择离线处理时，可以依赖自动测试证明原媒体、派生文本、向量、人物关系和创作者偏好没有发生未授权网络外发。

**Why this priority**：MemoLens 处理高敏感私人媒体；“默认本地”必须是可测试行为，不只是文案。

**Independent Test**：在网络监测和 deny 环境中完成索引、检索、记忆确认、故事生成与本地预览；未授权连接数应为零。

**Acceptance Scenarios**：

1. **Given** offline profile，**When** 完成全部核心旅程，**Then** 不产生任何非 loopback 网络连接。
2. **Given** 一个需要远端 provider 的操作但没有有效授权，**When** 适配器准备发送，**Then** 操作在发送前失败，并记录不含私人内容的原因。
3. **Given** 一个明确授权的远端实验，**When** 请求完成，**Then** 工件列出 provider、model、payload class、asset scope、字节数、hash、时间和结果状态。

### User Story 3：研究者定位失败属于哪一层（Priority: P1）

研究者能分别评估候选生成、约束融合、排序、拒答、文案生成和证据引用，避免把一个端到端分数错误归因给模型。

**Why this priority**：PhotoBench 的 source fusion paradox 表明单项召回强并不代表组合可靠；必须观察中间候选和裁剪过程。

**Independent Test**：使用预计算视觉/文本特征运行 planner/fusion-only 评测，再使用固定 query plan 运行 retriever-only 评测。

**Acceptance Scenarios**：

1. **Given** 同一查询的 source-specific candidate sets，**When** 融合后丢失 ground truth，**Then** 报告指出哪条约束或集合操作排除了它。
2. **Given** retrieval 找到正确素材但生成文案包含无证据陈述，**When** 评分，**Then** retrieval 与 generation 得分分开记录。
3. **Given** 无正确结果查询，**When** 系统返回候选，**Then** 记录 false positive，而不是把任意相似素材视为成功。

### User Story 4：产品负责人管理可公开声明（Priority: P2）

产品负责人可以查看每条质量、隐私、性能或创新性声明对应的数据版本、运行环境、指标、阈值和原始工件。

**Why this priority**：项目已有复杂功能，但尚缺可审计的结果声明治理。

**Independent Test**：选择一条文档或发布文案中的结果型声明，能够追踪到唯一 benchmark run 和不可变 manifest。

**Acceptance Scenarios**：

1. **Given** 一条“检索提升 20%”声明，**When** 审查声明 registry，**Then** 可确定相对哪个 baseline、哪个指标、哪些 query bucket 和哪次运行。
2. **Given** 指标未达到预注册阈值，**When** 生成 release summary，**Then** 声明保持“实验目标”，不能升级为“项目结果”。

## Edge Cases

- 查询存在多个同等正确结果，且顺序不唯一。
- 查询依赖时区、夏令时、模糊日期或“上次旅行之前”等相对时间。
- 查询把错误记忆当作事实，正确行为是拒答或澄清。
- 图片为 burst、near-duplicate、低光、旋转、损坏或缺失 EXIF。
- 视频事件跨镜头、非常短、只有音频证据或字幕与画面冲突。
- 中英混合、同义改写、人物昵称、拼写错误和 CJK 分词。
- 模型 unavailable 后发生 fallback，结果看似成功但语义能力下降。
- 同一数据在不同硬件或运行时产生细微浮点差异。
- benchmark 数据包含私人路径、EXIF、face embedding 或真实姓名。
- 远端 provider 接收请求失败、重试、超时或返回 partial response。
- 某次运行只完成部分 query，或进程在写工件时崩溃。
- 指标平均值提高，但小语种、无答案或多来源 bucket 退化。

## Requirements

### Functional Requirements

- **FR-001**：系统必须为每次 benchmark run 创建不可变 run identity，并记录代码 revision、数据 manifest hash、配置、模型/预处理版本、运行环境和随机种子。
- **FR-002**：系统必须区分公开/合成数据、经同意的私有数据和禁止持久化的数据；真实私人媒体及其可逆派生物不得提交到仓库。
- **FR-003**：系统必须保存 query、允许的 ground truth 表达、无答案标记、任务类别、语言、媒体类型、来源数量和难度标签。
- **FR-004**：照片检索必须至少覆盖视觉、时间、地点、人物/关系、事件、质量、否定、多结果和无答案查询。
- **FR-005**：视频评测必须以 source asset 和时间范围作为 ground truth，并报告 temporal overlap 与 retrieval 指标。
- **FR-006**：Creator Memory 评测必须覆盖确认、修正、冲突、有效时间、项目范围、撤销、遗忘和拒绝未确认推断。
- **FR-007**：故事评测必须分别计算素材 grounding、claim evidence coverage、引用正确性和不可重放/无证据陈述。
- **FR-008**：系统必须分别评估 candidate generation、constraint fusion、ranking/selection、abstention 和 generation，不得只保留端到端总分。
- **FR-009**：系统必须保存每个检索源的候选、分数、命中约束和候选被排除的原因，且不得在工件中泄露真实私人路径。
- **FR-010**：系统必须按单来源、双来源、三来源及以上分别报告结果，以暴露 source fusion degradation。
- **FR-011**：系统必须报告至少 nDCG@10、Recall@k、precision/F1、无答案误返回率、延迟分位数；适用时报告 duplicate precision、diversity、calibration 和 temporal IoU。
- **FR-012**：性能结果必须包含 corpus size、媒体时长、硬件、并发、冷/热状态、p50/p95、peak RSS、磁盘与处理时间。
- **FR-013**：隐私测试必须监测所有非 loopback 网络活动，并区分 DNS、模型下载、遥测、provider payload 和用户明确授权请求。
- **FR-014**：每次远端测试必须记录 disclosure manifest；manifest 不得包含原始媒体内容，但必须足以核对发送范围。
- **FR-015**：删除/遗忘测试必须使用版本化 deletion-closure policy matrix，逐项验证 source authority、canonical identity、analysis、projection、embedding、caption、thumbnail、关系、缓存、训练集、备份、日志与导出引用应被保留、删除或 tombstone 的预期闭包。
- **FR-016**：系统必须检测 incomplete、corrupt 或不可比较的 run，并禁止其成为 release baseline。
- **FR-017**：所有结果型声明必须引用唯一 run、metric、baseline 和 scope；未引用者视为未验证。
- **FR-018**：benchmark 必须支持固定种子的重复运行，并报告均值、离散度或确定性偏差。
- **FR-019**：模型、embedding space、预处理或数据 schema 改变时，系统必须要求显式迁移或新 baseline，不能静默混算。
- **FR-020**：项目必须保留当前实现 baseline，包括 lexical mixed retrieval、legacy photo retrieval、Atlas duplicate/layout 和 Photo/Video 关键旅程。
- **FR-021**：外部 benchmark adapter 必须记录来源、版本、许可证和本地修改；不能把外部测试集结果外推成全部用户场景。
- **FR-022**：报告必须同时显示提升与回归，并把 P0 安全/隐私失败作为硬失败，不允许被平均分抵消。
- **FR-023**：每个前沿实验必须预注册假设、baseline、通过阈值、kill criteria 和降级路径。
- **FR-024**：另一位维护者必须能仅凭 manifest 和说明复跑非私有 benchmark，并得到预设容差内的结果。
- **FR-025**：每次发布基线必须冻结 production action inventory 的内容 hash；inventory 覆盖 HTTP、IPC、MCP、Bot、worker 与 export action，并记录 capability class，使新增 action 无法在不进入测试全集的情况下声称 100% 覆盖。
- **FR-026**：每个实验的 preregistration manifest 必须在运行前冻结 primary metric、公式、分母、样本/用户统计单位、paired fixture、置信区间方法、重复次数、硬件 profile、canonicalization、延迟类别、资源容差及 promote/continue-shadow/kill 三段决策；运行后不得从备选指标中择优。
- **FR-027**：Deletion-closure matrix 至少遵守以下语义：archive 只改变可见性而不删除 canonical history；source unavailable/revoke 阻止新读取并保留最小历史 identity；provider grant revoke 清除未发送 payload cache 且不宣称召回已外发 bytes；Creator full forget 删除 preference value/evidence/adapter/training references，仅允许 opaque ID/status/time tombstone；media delete 清理应用拥有的派生物，外部原文件只在单独明确授权时删除；用户已经导出或外部分发的副本标记为不可由本地闭包召回。

### Key Entities

- **Benchmark Suite**：一组版本化任务、数据边界、指标和发布门槛。
- **Dataset Manifest**：每个样本、query、ground truth、许可级别和内容 hash 的清单。
- **Query Case**：查询文本、结构标签、正确结果、无答案状态和难度。
- **Evidence Span**：图片区域、asset、视频时间段、transcript 或 metadata 证据范围。
- **Run Manifest**：代码、模型、配置、数据、环境、种子和执行状态的不可变描述。
- **Action Inventory Manifest**：全部 production surface/action、capability class、contract version 与内容 hash 的冻结清单。
- **Deletion-closure Policy Matrix**：按动作和实体声明 retain/delete/tombstone/not-controlled 及完成 SLA 的版本化矩阵。
- **Metric Record**：指标名称、范围、值、置信区间或容差、baseline 和 bucket。
- **Privacy Observation**：网络目的地、payload class、授权、hash、字节数和结果，不保存私人 payload。
- **Claim Registry Entry**：公开声明、对应 run、指标、范围、审核状态和失效条件。
- **Regression Waiver**：只允许记录明确期限、范围、负责人和原因的例外；不能豁免 P0 隐私门槛。

## Success Criteria

### Measurable Outcomes

- **SC-001**：公开/合成 benchmark 至少包含 300 个照片查询、100 个视频时间查询、100 个记忆/故事/拒答案例，并覆盖中英与混合查询。
- **SC-002**：100% query case 具有任务标签、ground truth 规则、许可级别和稳定 ID。
- **SC-003**：第二位维护者在冻结环境中复跑，确定性指标完全一致；允许浮点指标差异不超过预注册容差。
- **SC-004**：offline profile 的核心旅程非 loopback 网络连接数为 0。
- **SC-005**：无有效授权的 provider payload 发送数为 0；明确授权请求的 disclosure manifest 覆盖率为 100%。
- **SC-006**：冻结 action inventory 中 100% production action 均有对应 capability/privacy contract test，全部 P0 negative tests 通过率为 100%，没有 waiver；inventory hash 变化而测试映射未变化时 run 无效。
- **SC-007**：所有 release 结果型声明对 run/metric/baseline/scope 的可追踪率为 100%。
- **SC-008**：报告能分别定位 candidate、fusion、ranking、abstention 和 generation 失败；按固定 seed 分层抽查 `min(30, 当次全部失败数)` 个失败案例，归因缺失为 0；失败少于 30 个时必须检查全部失败。
- **SC-009**：三次固定种子实验均保存完整工件；任一 incomplete run 不进入 aggregate。
- **SC-010**：现状 baseline、候选版本和回退版本均能在同一报告中比较，且回退版本不依赖候选数据迁移才能运行。

## Baselines and Release Gates

首个 baseline 必须记录现状，不以好坏为前提：

- mixed image/video lexical fallback。
- legacy photo hybrid/MMR retrieval。
- `semantic_hash` text-derived fallback。
- Atlas duplicate、cluster 与 layout determinism。
- Photo Creator context prompt path。
- Video brief/timeline/render path。
- Offline、remote-query、remote-photo-analysis 三种网络 profile。

任一后续 spec 可以定义更严格门槛，但不能降低以下全局门槛：未授权 egress 为零；结果工件可复现；不同 embedding space 不混算；无答案被单独计分；按来源数量报告；P0 回归不能被平均分覆盖。

Deletion-closure matrix 是发布工件，不是实现后的解释性文档。每一行必须包含触发动作、实体类别、预期终态、active-context 停用时限、后台删除时限、备份恢复阻断策略和系统无法控制的外部副本说明。

## Out of Scope

- 本规范不选择向量数据库、模型、LLM、图数据库或 UI 测试框架。
- 本规范不授权收集用户私人相册作为公共测试集。
- 本规范不保证外部 benchmark 代表全部 MemoLens 用户。
- 本规范不定义营销文案，只定义声明需要的证据。
- 本规范不实现后续 007 至 013 的功能。

## Assumptions

- 项目可生成不包含真实私人信息的合成 photo/video fixtures。
- 外部数据集许可证会在下载和发布前单独审查。
- 部分高级模型只在特定硬件存在，报告会区分 mandatory baseline 与 optional track。
- 当前日期之后出现的新模型或 benchmark 不自动替换冻结 baseline。
- 对私有数据的本地运行工件默认不离开用户设备。

## Dependencies

- 当前测试与 CI 可以作为执行入口，但不能替代本规范的质量和隐私门槛。
- Spec 007 至 013 的结果型成功标准均依赖本规范产生的冻结 suite。
- Spec 005、006 对 Spec 004 的历史引用从本文件开始有明确仓库位置；此前声明不被自动追认。
