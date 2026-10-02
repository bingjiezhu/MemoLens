# Feature Specification: Hierarchical Temporal Memory

- Feature ID：`ML-010`
- 创建日期：2026-08-20
- 状态：`PROPOSED / EXPERIMENT`
- 实施授权：`NONE`
- 组合决策：[SHOULD-EXPERIMENT](../implementation-decisions-2026-08-20.md)；先验证精确时间证据与可修正事件，full event/episode/concept graph 不进入已承诺范围
- 规范类型：Spec Kit 风格的产品—架构混合规范
- 证据截止：2026-08-20
- 优先级：P1 能力跃迁
- 依赖：[Spec 004](../004-evidence-backed-retrieval-privacy-benchmark/spec.md)、[Spec 007](../007-local-capability-boundary/spec.md)、[Spec 008](../008-unified-media-memory-kernel/spec.md)、[Spec 009](../009-intent-evidence-retrieval/spec.md)
- 研究依据：[长视频与时间证据](../../frontier-research-2026-08-20.md#8-长视频与时间证据)

## Overview

MemoLens 应把媒体记忆组织为多时间尺度的证据层：原始 asset、图片观察或视频片段、事件、跨媒体 episode 与长期概念。每一层都必须回到具体 asset 或视频时间范围，并记录由什么信号、模型、规则或用户确认形成。

系统不默认把全部媒体压缩成文字摘要，也不默认为整个 library 建全量知识图。简单视觉查询直接使用局部 evidence；只有事件、多跳、长视频或低置信查询才展开更高层记忆并定向回看原始片段。

## Hypothesis and Falsification

### Hypothesis

相比固定间隔抽帧、整段视频摘要和每次全量二维聚类，镜头/片段/事件/episode 分层、跨模态边界与 query-guided refinement 能以更低处理成本找到更准确的时间证据，并支持“那次旅行”“晚饭前”“她第一次出现”等生活记忆查询。

### Falsification / Kill Criteria

- 在固定私人视频/合成时间定位集上，primary `R@1@IoU≥0.5` 相对最强基线提升低于 5 个百分点，或 mean tIoU 退化超过 2 个百分点，则终止该策略；提升 5 至 20 个百分点只继续 shadow，达到 20 个百分点且全部 guardrail 通过才 promote。
- 压缩后 Recall 降幅超过 5 个百分点，回退为分级自适应采样。
- 全事件图在多跳/时间更新集上不优于类型化关系表，取消全图层，只保留事件与关系 projection。
- 增量更新仍需要全量 N×N 相似度矩阵或正常单资产更新随库规模线性增长，则 Atlas/episode 聚合设计失败。

## User Scenarios & Testing

### User Story 1：用户找到视频中的具体时刻（Priority: P1）

用户搜索“祖母拥抱孩子后大家一起吹蜡烛”，结果直接定位到相关时间段，并显示画面、语音、OCR 或事件证据，而不是只返回整条视频。

**Why this priority**：精确时间证据是视频进入检索、故事和可编辑时间线的基础。

**Independent Test**：用有人工时间范围标注的查询，比较固定抽帧、固定 segment 和分层 refinement 的 R@k、tIoU 与边界误差。

**Acceptance Scenarios**：

1. **Given** 事件只持续数秒，**When** 用户搜索，**Then** 结果包含稳定 start/end 范围和主要 evidence。
2. **Given** 语音与画面共同确定事件，**When** 召回，**Then** 两种 source 分别可追溯，摘要不替代原始范围。
3. **Given** 事件跨越两个镜头，**When** 形成结果，**Then** 时间范围可包含相邻片段或明确的组合 evidence。
4. **Given** 只有整段摘要匹配但局部证据不足，**When** 返回，**Then** 结果标记为 episode-level hypothesis，不假装精确定位。

### User Story 2：用户以事件浏览跨照片与视频的记忆（Priority: P1）

用户打开“一次生日”“那趟海边旅行”或某日 episode，可以看到相关照片、视频片段、地点和时间顺序；系统能区分同一天的不同事件和跨天的同一旅行。

**Why this priority**：私人媒体的自然组织单位通常是生活事件，不是文件夹或二维 cluster。

**Independent Test**：使用带事件边界、跨媒体成员和 distractor 的合成/许可 library，评估 event purity、coverage、chronology 和可解释 membership。

**Acceptance Scenarios**：

1. **Given** 同一地点同一天发生两个不同活动，**When** 构建事件，**Then** 不仅按时间/地点粗暴合并，视觉与内容变化可分开它们。
2. **Given** 一次旅行跨多天和多个地点，**When** 查看 episode，**Then** day event 可组成更高层 episode，并保留各自边界。
3. **Given** 用户把一张照片移出事件，**When** 修正，**Then** 该决定被记录并在后续 rebuild 保持，不删除原始自动 hypothesis。
4. **Given** 事件成员置信不足，**When** 显示，**Then** 系统将其标为候选，不把推断当作确认事实。

### User Story 3：用户用相对时间和事件关系检索（Priority: P1）

用户可以询问“登机前最后一顿饭”“孩子第一次上台之后”“我们搬家那周”，系统把事件时间、先后关系和原始 evidence 结合起来。

**Why this priority**：长期记忆价值来自事件关系和时间修正，不只是 caption 相似度。

**Independent Test**：建立 before/after/during/first/last/overlap 和相对日期 query，包含相机时间错误与后来修正。

**Acceptance Scenarios**：

1. **Given** 已确认事件锚点，**When** 查询“之前”，**Then** 时间窗口可追溯到锚点和关系规则。
2. **Given** EXIF 时间与用户确认时间冲突，**When** 查询，**Then** 两种 assertion 均保留，当前选择与置信明确。
3. **Given** “第一次”需要证明此前没有同类 evidence，**When** 数据覆盖不足，**Then** 系统使用限定表述或拒绝绝对结论。
4. **Given** 时区或相机时钟偏移修正，**When** 重算关系，**Then** 旧 observed value 保留，current event time 可更新。

### User Story 4：系统只在需要时回看高成本视觉细节（Priority: P1）

日常浏览和搜索使用已有轻量 memory；复杂问题只对少量候选片段执行更密集的帧、音频或多模态复核，且遵守本地/远端预算。

**Why this priority**：长视频不能每次查询都重跑全片模型。

**Independent Test**：对 simple/hard query 记录读取片段、帧/token、模型调用、延迟与 Recall。

**Acceptance Scenarios**：

1. **Given** 现有片段证据足够，**When** 查询，**Then** 不打开原始长视频做额外分析。
2. **Given** 候选时间范围宽且问题依赖视觉细节，**When** refinement，**Then** 只复核受限窗口，并记录从 episode 到原片段的路径。
3. **Given** refinement 达到预算，**When** 仍不确定，**Then** 返回不确定或请求澄清，不扫描全部视频。
4. **Given** offline profile，**When** 远端能力不可用，**Then** 保持本地 evidence 和明确降级。

### User Story 5：维护者增量更新记忆而不重建全库（Priority: P1）

新增、移动、删除或重分析一个 asset 时，系统只更新受影响的片段、事件、邻居和 projection；定期全量 compaction 用于验证与整理，不是每次操作的必需步骤。

**Why this priority**：当前 Atlas 全量 dense matrix 无法扩展到大型私人库。

**Independent Test**：在 1k/10k/100k synthetic corpus 中新增、修改、删除单资产，比较增量与 full build 的工作量和结果等价性。

**Acceptance Scenarios**：

1. **Given** 新增一段视频，**When** 更新 memory，**Then** 不分配与全库资产数平方相关的结构。
2. **Given** 一张照片时间被修正，**When** 更新事件，**Then** 只重评相关时间邻域和受影响 episode。
3. **Given** 增量结果偏离 full build 超过容差，**When** 检测，**Then** generation 标记需 compaction，而不是静默继续。
4. **Given** model/algorithm version 变化，**When** 当前 projection 被检查，**Then** 正确标记 stale 并构建新 generation。

## Edge Cases

- 几毫秒闪现、黑场、慢动作、延时摄影和可变帧率。
- 音画不同步、静音、音乐盖过语音或 ASR 时间戳漂移。
- 一次事件跨多个文件、Live Photo 或相机自动切片。
- 多个相机拍摄同一事件，时钟不同步。
- 同一日期大量 burst、截图、转发图和无 EXIF 文件。
- 旅行跨时区，EXIF 无 offset 或手机时区后来变化。
- OCR、ASR、视觉和 metadata 对事件内容互相冲突。
- 用户确认事件成员后，源 asset 被 archive、移动或删除。
- 长摘要遗漏一个短但关键的视觉细节。
- 高层 concept 更新后错误反向污染原始 observation。
- Episode 层级过深、循环 membership 或一项属于多个有效 episode。
- “第一次”“最后一次”“总是”等需要全库覆盖证明的绝对词。
- Person-sensitive 能力被用户关闭，事件仍需在无身份条件下工作。
- 增量 update 与 full rebuild、library switch 或 model migration 并发。

## Requirements

### Functional Requirements

- **FR-001**：系统必须为所有时间证据记录稳定 asset/segment identity、start/end 或 point time、source、confidence、revision 和 observation time。
- **FR-002**：图片 observation、视频 shot/segment、event、episode 和长期 concept 必须是可区分层级；高层记录不能替代低层 evidence。
- **FR-003**：每个高层 summary、event membership 和关系必须保留到一个或多个原始 evidence span 的反向引用。
- **FR-004**：视频边界必须能组合视觉变化、运动、音频、ASR、OCR、人物或其他已授权信号；缺失某信号不能使整个分析失败。
- **FR-005**：Boundary、summary、membership 和 relation 必须分别记录 provenance 与置信，不能共用一个不透明“模型分数”。
- **FR-006**：系统必须支持多时间尺度检索，并让 query 指定或推断适当粒度。
- **FR-007**：精确视频结果必须返回时间范围；只有 episode-level evidence 时不得伪造精确范围。
- **FR-008**：相邻或跨文件 span 可以组成一个 event，但组合关系和 gap 必须显式记录。
- **FR-009**：Event 可以跨照片与视频；membership 必须基于 evidence 或用户确认，不以同一日期作为唯一依据。
- **FR-010**：系统必须区分自动 event hypothesis、用户确认、用户排除和后续 supersede。
- **FR-011**：时间 assertion 必须区分媒体记录时间、推断有效时间、系统观察时间和用户修正时间。
- **FR-012**：新修正不得物理覆盖旧 assertion；current resolution 和 supersedes/invalidates 必须可追溯。
- **FR-013**：相对时间、before/after/during/overlap/first/last 必须通过显式关系和覆盖条件解释。
- **FR-014**：无法证明绝对“第一次/最后一次/从不”时，系统必须限定范围或拒绝绝对表述。
- **FR-015**：高层 memory 必须支持 query-guided refinement 到受限原始窗口，并记录触发原因、预算和新增 evidence。
- **FR-016**：Refinement 必须有帧/token、时长、模型调用、隐私和迭代预算。
- **FR-017**：达到预算仍不确定时不得扩大为默认全库重分析。
- **FR-018**：Offline profile 下所有 refinement 保持本地；远端复核受 Spec 007 grant 约束。
- **FR-019**：新资产或单资产变化必须支持增量 neighborhood/event/projection 更新；单资产更新的耗时和峰值内存不得随总库规模呈平方增长。具体索引和邻域表示留给技术计划。
- **FR-020**：增量与 full build 必须有等价性指标、偏差容差和 compaction 触发条件。
- **FR-021**：Projection rebuild 的并发行为必须保证用户只读到一个完整有效 generation，重复请求不能产生相互冲突的有效结果；具体并发机制留给技术计划。
- **FR-022**：Layout、cluster、event、model 和 algorithm version 变化必须使相关 projection 正确 stale。
- **FR-023**：展示 layout 只负责可视化，不得反向定义语义 cluster 或 duplicate 真值。
- **FR-024**：不同 embedding space 或不同 sensitive policy 的节点不得直接混入同一相似图。
- **FR-025**：系统必须支持 event/episode archive、merge、split、rename、member correction 和 undo，并保留历史。
- **FR-026**：删除、撤销或 source unavailable 必须传播到 current memory view，同时保留允许的 provenance tombstone。
- **FR-027**：所有 memory query/result 必须兼容 Spec 009 intent/evidence contract。
- **FR-028**：Event graph 只能在 benchmark 证明有收益的关系上启用；不要求为每个标签建立边。
- **FR-029**：用户确认的人物/关系 evidence 与自动视觉 hypothesis 必须分级，后者不能静默成为 hard identity。
- **FR-030**：每个 analysis/profile 变更必须可在 Spec 004 上比较时间定位、event quality、成本和隐私。

### Key Entities

- **Temporal Evidence Span**：某 asset 上的时间点或范围及来源。
- **Shot/Segment**：具有稳定边界、局部 evidence 和分析 revision 的视频单元。
- **Event Hypothesis**：由多个 evidence span 支持、尚可修正的生活事件。
- **Episode**：由相关事件组成的更长时间范围，例如旅行或项目周期。
- **Semantic Memory**：从多个事件形成的长期概念，不替代具体事件。
- **Temporal Assertion**：有效时间、观察时间、来源和 supersede 状态明确的陈述。
- **Membership**：asset/segment/event 属于某 event/episode 的带 provenance 关系。
- **Refinement Request**：针对受限时间窗口的额外证据获取操作。
- **Memory Generation**：某版本算法、模型、ledger position 和 policy 下的完整 projection。
- **Compaction**：用 full build 验证、整理增量 projection 的有界操作。

## Success Criteria

### Measurable Outcomes

- **SC-001**：至少 100 个私人许可/合成视频查询上，primary `R@1@IoU≥0.5` 相对现有 segment、固定 1fps 与 compute-matched adaptive sampling 中最强 baseline 提升至少 20 个百分点；mean tIoU 不低于最强 baseline 2 个百分点以上，并同时报告 `R@1@IoU≥0.3/0.7`、temporal mAP 和 boundary error。
- **SC-002**：Initial ingest 的 keyframe 数相对固定 1fps decode baseline 减少至少 90%，visual token 数相对“固定 1fps、同分辨率逐帧编码”baseline 另行减少至少 90%；两个分母不得混用，且 `R@1@IoU≥0.5` 下降不超过 3 个百分点。
- **SC-003**：精确事件时间边界误差中位数不超过 2 秒；跨镜头事件按组合 span 单独计分。
- **SC-004**：100% summary、event 和 episode result 能反向定位到一个或多个稳定 evidence span；人工分层抽样至少 200 个引用，引用与标注范围 temporal IoU≥0.5 的比例及语义 evidence correctness 均至少 95%。
- **SC-005**：Predicted event 与 ground-truth event 以最大权重二分匹配，权重为 member F1，匹配阈值 0.5；允许一个 asset 参与多个不同层级 episode。匹配后的 micro/macro member precision 与 recall 均不低于 0.90，用户确认 member 的 rebuild 保留率为 100%。
- **SC-006**：时间修正/冲突集上的 current assertion 准确率至少 98%，过期事实误用率低于 2%。
- **SC-007**：简单 query 不触发原始视频 refinement 的比例至少 95%；困难 query refinement 只读取预注册候选预算内的数据。
- **SC-008**：GPU profile ingest wall time不超过媒体时长的 0.5 倍；CPU-only profile不超过 2 倍，具体硬件由 Spec 004 冻结。
- **SC-009**：Analysis 已就绪后的单资产 projection 增量更新在 1k/10k/100k corpus 上 p95 分别不超过 0.5/1.5/4 秒，额外 peak RSS 不超过 256 MiB，100k 对 10k 的延迟增长不超过 3 倍；任一规模不分配 N×N dense matrix。
- **SC-010**：与同版本 full build 相比，增量 kNN 邻居 recall 至少 0.98、event member F1 至少 0.98；任一低于门槛时自动标记 compaction，不静默保持 current。
- **SC-011**：确定性 conformance track 在三个独立进程中对 entity ID、source membership 和离散 relation 输出 exact hash 一致；允许随机/GPU 浮点的 quality track 不要求 bitwise 一致，但 `R@1@IoU≥0.5` 与 event F1 跨运行差异不超过 0.5 个百分点，边界中位差不超过 50ms，展示布局只按预注册容差比较。
- **SC-012**：Offline profile 的非 loopback 请求为 0；远端 refinement 的 grant/disclosure 覆盖率为 100%。

## Baseline and Experiment Protocol

至少比较：固定时间抽帧、现有 segment 逻辑、视觉边界、音画联合边界、层级 memory、query-guided refinement。Event 侧至少比较时间/地点规则、类型化关系表和受限图 projection。

报告必须分开：segment proposal recall、boundary quality、retrieval、event membership、summary evidence coverage、refinement gain、frames/tokens、ingest cost 和 query latency。不能用最终 QA 分数掩盖时间范围错误。

决策分三段：primary 提升低于 5 个百分点或任一非劣 guardrail 失败时 kill；提升 5 至 20 个百分点保持 shadow 并继续分析；达到 20 个百分点、95% paired CI 下界大于 0 且全部质量/成本/隐私门槛通过才 promote。

## Rollback and Degradation

- 层级 memory 作为新 projection 构建，不覆盖旧 analysis。
- 新 boundary/event generation 未通过门槛时继续使用旧验证 generation。
- Refinement 失败时保留已有粗粒度 evidence，并明确精度等级。
- Full graph 无收益时退回事件与关系表，不保留图复杂度。
- 压缩回归时增加候选采样或回到分级自适应采样，不静默降低 Recall。
- Source unavailable 时保留 tombstone 和旧 project provenance，但 current browse 不展示不可访问媒体。

## Out of Scope

- 不定义具体视频模型、ASR、OCR、聚类、图数据库或 ANN 实现。
- 不保证从媒体推断真实人物身份或敏感关系。
- 不把 summary 当作事实真值。
- 不要求一次构建全库知识图。
- 不在本规范中决定故事节奏或 Creator preference。

## Assumptions

- Spec 008 提供不可变 analysis、segment 和 projection generation。
- 用户可以关闭 people-sensitive analysis，基础时间记忆仍可工作。
- 部分事件需要用户确认，自动 clustering 只产生 hypothesis。
- 精确时间指标使用许可或合成标注数据，不使用未同意的私人媒体公开评测。
- 性能门槛按固定设备 profile 报告，不跨硬件直接比较。

## Dependencies

- Spec 004 提供时间 query、tIoU、成本、determinism 与隐私 benchmark。
- Spec 007 管理原视频读取和远端 refinement capability。
- Spec 008 提供 canonical evidence、operation、model space 和 projection generation。
- Spec 009 提供 query clause、retriever、fusion、budget 和 result evidence contract。
- Spec 011 可以消费 event feedback，但不能覆盖原始 evidence。
