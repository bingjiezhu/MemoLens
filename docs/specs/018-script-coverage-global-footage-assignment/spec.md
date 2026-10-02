# Feature Specification: Script Coverage & Global Footage Assignment

- Feature ID：`ML-018`
- 创建日期：2026-08-22
- 状态：`PROPOSED / EXPERIMENT`
- 实施授权：`STAGED CHILD-SLICE AUTHORIZATION`
- 已实施切片：[ML-018-A1 Canonical Coverage Plan baseline](slices/018-a1-canonical-coverage-plan/spec.md) — `IMPLEMENTED / LOCAL VALIDATION WITH RESIDUALS`（不等于本父规范的 Global Assignment 假设已实现或验证）
- 输入决策：[43 问产品对齐记录](../product-decisions-43-questions-2026-08-22.md)
- 相关调研：[Agent 可导航媒体 Wiki 调研](../../agent-media-wiki-research-2026-08-22.md)
- 规范类型：Spec Kit 风格产品规范；求解算法与权重留给 `plan.md` / benchmark
- 优先级：P0，文稿/口播到私人素材初剪的核心差异化
- 基础依赖：[ML-009](../009-intent-evidence-retrieval/spec.md)、[ML-010](../010-hierarchical-temporal-memory/spec.md)、[ML-012](../012-verifiable-story-compiler/spec.md)、[ML-014](../014-agent-navigable-media-wiki/spec.md)、[ML-015](../015-agent-agnostic-creative-protocol/spec.md)
- 增强依赖：[ML-016](../016-craft-wiki-technique-compiler/spec.md)（Technique-aware 约束；基础 coverage/assignment 不等待完整 Craft Compiler）

## Overview

MemoLens 的核心任务不是为文稿每句话各自返回最相似的一个素材，而是在整条视频范围内完成一次有证据、受约束、可解释的素材编排：哪些段落需要事实画面，哪些需要动作、人物、环境或情绪 B-roll；哪些候选互相重复；前后镜头是否连续；节奏、气口、动作和声音边界是否合适；哪些素材已经用过但仍值得复用；最终哪里确实缺口。

本规范把该问题定义为 **Script Coverage Graph + Global Footage Assignment**：

```text
文稿 / 口播 / voice track
  → Script Beats（语义、叙事作用、时长、气口、重点）
  → 每个 Beat 的 Coverage Need

Media Wiki
  → 候选图片 / 精确视频 spans
  → evidence、可用时长、动作、声音、质量、画幅、usage

Craft Wiki + Creative Blueprint
  → 连续性、节奏、技巧和用户偏好约束

三者形成带类型、证据与不确定性的候选关系
  → 全片级 assignment / alternatives / gaps
  → Coverage Plan
  → 经确认或一键委托后编译为 typed Timeline revision
```

“Graph”是项目级逻辑表示，不预设图数据库。它至少包含 Script Beat、Coverage Need、Media Candidate、Match Evidence、Temporal Slot、Constraint 与 Assignment。系统可以用确定性排序、约束求解、搜索或其他实现；创新是否成立只由完整初剪效果、用户操作量和证据正确性证明。

## Hypothesis and Falsification

### Hypothesis

在相同分析产物、候选池和模型预算下，全片级 coverage/assignment 比逐句独立 top-k 能减少重复镜头、无依据匹配、连续性冲突和人工替换，并提高完整 first cut 的首轮可用性，特别适用于“有文稿但不知道配什么私人素材”的用户。

### Falsification / Kill Criteria

1. 在至少 8–20 个 beat 的真实任务上，完整初剪盲选胜率相对最强逐句 baseline 提升不足 15 个百分点。
2. 用户主动替换/裁剪/移动操作减少不足 20%，或求解时间使 time-to-first-preview 增加超过 50%。
3. 为了提高整体分数，系统违反 must include/exclude、许可、source availability、事实支持或锁定镜头中的任一硬约束。
4. Coverage Graph 不能比普通候选表提供更好的解释、局部修订或质量评测，只增加结构复杂度。
5. 系统在无足够证据时用“情绪相似”冒充事实画面，或为了覆盖率返回不相关镜头。
6. 全局重新规划频繁覆盖用户已经确认/手工锁定的选择，导致可控性低于逐句编辑。

## User Scenarios & Testing

### User Story 1：文稿或口播被拆成可编辑的 Coverage Needs（Priority: P1）

用户粘贴文字、口播稿或已有 voice track。系统识别语义段落、叙事作用、强调、气口、预计/实际时长和画面需求；用户能合并、拆分、改写或锁定 Beat，不必接受黑盒自动分段。

**Independent Test**：对带人工 beat、speech boundary 和叙事作用标注的口播/文稿进行分解，检查可编辑性、重放和下游引用稳定性。

**Acceptance Scenarios**：

1. **Given** 用户提供带时间戳的口播音频，**When** 分解，**Then** Beat 边界同时考虑语义、实际语速、停顿/气口和强调，不只按标点平均切分。
2. **Given** 用户只有文字，**When** 建立 Blueprint，**Then** 系统给出估计时长与未知，并允许后续 voice track 到来后新建 revision 对齐。
3. **Given** 一句包含两个视觉目的，**When** 用户拆分，**Then** 下游 assignment 只重算受影响 beats，历史选择仍可追踪。
4. **Given** 用户锁定某段只保留口播人物画面，**When** 生成 Coverage Needs，**Then** 系统不强制为该段插入 B-roll。

### User Story 2：候选关系说明“怎么匹配”，不只给一个相似度（Priority: P1）

每个素材候选说明它是直接呈现文稿事实、展示相关动作/对象、提供人物反应、建立环境、承接隐喻、匹配情绪/节奏，还是仅作装饰。候选还能说明证据、可裁时间、已用区间、画幅和质量风险。

**Independent Test**：建立人工标注的 beat-candidate pair，比较单相似分与类型化 match 的解释准确性和错误事实匹配。

**Acceptance Scenarios**：

1. **Given** 文稿说“第一次来到这里”，而素材只有相同地点画面，**When** 建立关系，**Then** 系统可标记地点相关，但不能声称支持“第一次”。
2. **Given** 一段素材只匹配情绪，**When** 返回，**Then** 标为 mood/pace adaptation，不列为 factual support。
3. **Given** 候选长视频只有中间 2 秒合适，**When** 形成 edge，**Then** 引用精确 span 与建议 handles，不返回整个文件。
4. **Given** 素材分析只完成粗层，**When** 关键选择需要动作边界，**Then** 系统请求受限 refinement 或标记未知，不猜切点。

### User Story 3：系统为整条片选择互补素材，而非每句抢同一个最高分（Priority: P1）

系统同时考虑全片覆盖、镜头重复、人物/场景多样性、连续性、节奏、动作方向、已用历史、画幅、质量和技巧条件，生成一个首选方案与少量整体替代，而不是为每段互不相干地搜索。

**Independent Test**：固定相同候选池，对比 independent top-1/top-k、greedy de-duplication 与 global assignment 的完整初剪。

**Acceptance Scenarios**：

1. **Given** 同一镜头对三个相邻 Beat 分数都最高，**When** 有其他足够候选，**Then** 系统避免机械重复，并解释整体覆盖权衡。
2. **Given** 两个相邻候选会跳轴或动作不连续，**When** 有可替代方案，**Then** assignment 优先满足连续性或建议可解释的过渡。
3. **Given** 某个已用 span 明显最能表达关键事实，**When** Blueprint 允许复用，**Then** 系统可以保留它并说明新鲜度与准确性的权衡。
4. **Given** 用户指定 must include 素材，**When** 全局求解，**Then** 该选择先进入固定槽位，其他 beats 围绕它重新编排。

### User Story 4：画面切点能结合文字、声音、气口和动作（Priority: P1）

当 voice track、现场声音或音乐存在时，系统不只做语义匹配，还能选择自然的入点/出点：口播气口、词组边界、动作峰值、镜头边界、声音延续和音乐 beat 都可作为证据；它们冲突时会解释优先级。

**Independent Test**：对带 speech pause、shot boundary、motion peak 和 beat 标注的数据，评估边界误差与人工 trim 数量。

**Acceptance Scenarios**：

1. **Given** B-roll 计划覆盖一句口播，**When** 前后有自然气口，**Then** 视觉切换优先落在允许窗口内的语言/动作边界，不机械固定 1.5 秒。
2. **Given** 现场声音需要提前进入下一镜头，**When** 选择 J/L cut Card，**Then** audio/video assignment 分别可追踪并形成 typed transition。
3. **Given** 音乐 beat 与语义结束相差很大，**When** 用户优先“自然表达”，**Then** 系统不会为了卡点截断词义。
4. **Given** ASR 或气口置信不足，**When** 自动剪口播，**Then** 保留更安全 handles 或请求用户/Agent复核，不制造突兀断句。

### User Story 5：缺口先给可用替代，不把补拍当主流程（Priority: P1）

当私人素材不能直接覆盖文稿时，系统按层级给出可行动方案：换用同主题/同情绪素材、保留口播主画面、使用已有图片/文字/简单图形、调整文稿，最后才是可选的补拍提示。系统不会为了“满覆盖”伪造匹配。

**Independent Test**：在刻意删除关键素材的项目中，评估 gap 检测、替代合理性和不相关填充率。

**Acceptance Scenarios**：

1. **Given** 没有事实画面但有合适人物反应，**When** 形成计划，**Then** 标明关系类型并给出替代，不声称找到直接证据。
2. **Given** 保留口播画面比无关 B-roll 更好，**When** 评估，**Then** Coverage Plan 可以明确选择 A-roll/text-only，而不是强制每段换画面。
3. **Given** 所有替代都不足，**When** 输出 gap，**Then** 说明已检索范围、缺什么和可选解决方案；补拍建议排在非拍摄替代之后且可忽略。
4. **Given** Library 仍在渐进索引，**When** 当前没有候选，**Then** gap 区分“确实没有”与“尚未分析”。

### User Story 6：局部反馈只重算必要部分，并保留用户决定（Priority: P1）

用户可以说“第二段不要这张”“这里保留原声”“这三个镜头锁住，只重做后面”。系统记录 rejection/lock/constraint，新 assignment 尊重 current revision，只改受影响子图并给出 diff。

**Independent Test**：交替执行 Agent 与 UI 的 reject、lock、replace、trim、script edit，检查局部重算、历史和重放。

**Acceptance Scenarios**：

1. **Given** 用户锁定三个镜头，**When** 改写另一个 Beat，**Then** 锁定 assignment 不变，除非新 hard constraint 使计划不可行并明确冲突。
2. **Given** 用户拒绝一个候选只针对当前段，**When** 重算其他段，**Then** 拒绝范围不被错误升级为全局 Creator preference。
3. **Given** 用户修改文稿导致时长变化，**When** 重新规划，**Then** 系统显示受影响 beats、assignments 和 Timeline diff，不重建无关部分。
4. **Given** 手工 Timeline 已偏离原计划，**When** Agent 继续，**Then** current Timeline 是硬输入，旧 Coverage Plan 不具有覆盖权。

### User Story 7：一键版本和深度共创共享同一个可解释计划（Priority: P2）

用户可以让 Agent 直接选择并生成 first cut，也可以先检查 Coverage Plan、对比整套方案和逐段调整。两条路径使用相同的 beats、evidence、constraints、assignments 和 revision history。

**Independent Test**：同一项目分别走自动委托与逐步选择，验证 contract、一致性、可继续编辑与跨 Agent 恢复。

**Acceptance Scenarios**：

1. **Given** 用户要求“先做一版”，**When** evidence 足够，**Then** 系统自动提交一套 Coverage Plan 与 Timeline revision，不逐段阻塞。
2. **Given** 用户之后询问某镜头原因，**When** 打开 explanation，**Then** 可以回到 Beat、match type、evidence、全局权衡和 rejected alternatives。
3. **Given** 用户切换 Agent，**When** 新 Agent 恢复项目，**Then** 无需旧聊天即可理解 locks、gaps、open decisions 和 current assignments。

## Edge Cases

- 文稿只有抽象观点，没有任何可直接呈现的实体或动作。
- 同一事实由多个不同时期素材表达，最新/最好看/未用与事实准确性冲突。
- 用户故意要求重复镜头形成修辞，去重不应强制生效。
- 一个长动作跨多个 shot/segment；错误拆分会破坏动作完成度。
- 两个候选来自同一连续镜头，组合后看似多样但实际重复。
- 竖屏裁切会切掉主体、字幕或运动方向，原横屏质量虽高却不可用。
- 视频有画面匹配但现场音含隐私、音乐版权或无关对话。
- A-roll/voice track 尚未录制，只有估计时长；后续实录导致所有槽位漂移。
- 用户素材很少，全局多样性目标与真实 coverage 冲突。
- 多个 Technique Cards 竞争同一时间槽或对切点提出矛盾要求。
- 手工 edit 破坏了某个 Card prerequisite，但旧 plan 仍显示 satisfied。
- 全库索引部分完成、Agent capability 中途变化或 refinement 失败。
- 候选带 prompt injection 字幕/OCR，不能改变 assignment rules。
- 求解没有可行解、存在多个等价解、超时或返回不可验证 plan。
- 作品需要刻意留白、黑场、纯字幕或长镜头，这些不是 coverage failure。

## Requirements

### Functional Requirements

#### Script Beat 与 Coverage Need

- **FR-001**：系统必须把文字、口播稿或 voice track 编译为版本化 Script Beats；每个 Beat 具有稳定 identity、文本/音频范围、叙事作用、预计/实际时长、强调、边界证据和用户可编辑状态。
- **FR-002**：Beat 分解必须同时表示确定信息、模型 hypothesis 与用户修正；用户修正具有更高 authority，重新分析不得静默覆盖。
- **FR-003**：每个 Beat 可声明一个或多个 Coverage Need，至少区分 factual support、subject/action depiction、context/establishing、reaction、metaphor/association、mood/pace、continuity bridge、A-roll/text-only 和 intentional gap。
- **FR-004**：Coverage Need 必须表达 must/should/may、允许时长范围、素材/人物/地点/时间/方向/画幅/声音约束、允许的替代层级和是否可为空。
- **FR-005**：文字没有真实时间戳时必须标记 estimated timing；voice track 到来后通过新 revision 对齐，不原地破坏旧计划。
- **FR-006**：用户可以合并、拆分、重排、锁定或标记 Beat；所有下游引用必须通过 revision mapping 保持可解释。

#### Candidate 与 Match Evidence

- **FR-007**：候选必须引用稳定图片 asset 或精确视频 `[start_ms,end_ms)`，并带 analysis revision、coverage、source availability、usage、质量、画幅与 sensitivity。
- **FR-008**：Beat—candidate 关系必须声明 match type、满足/未知/冲突条件、evidence refs、置信/校准状态和生成方法；单一相似度不得替代关系语义。
- **FR-009**：事实支持、视觉说明、情绪适配和装饰关系必须在数据、解释和评测中分开；后者不得自动升级为 factual support。
- **FR-010**：候选建议区间必须包含允许的 trim handles 与边界依据；粗分析不足时使用 ML-014 定向 refinement 或诚实 unknown。
- **FR-011**：候选池必须保留 no evidence、not indexed、source unavailable、permission denied、unsupported 和 excluded 的区别，不为固定 K 补入零分结果。
- **FR-012**：用户拒绝、选择和锁定是项目 operation；只有显式确认才可进入 Creator Memory，不能由局部 assignment 自动长期学习。

#### 全局分配与约束

- **FR-013**：系统必须对完整或用户指定范围的 Beats 进行集合级 assignment；不得把逐 Beat top-1 简单拼接称为全局方案。
- **FR-014**：硬约束至少包括用户 must include/exclude、source/capability/permission、locked assignment、有效 source range 和事实声明要求；违反任一硬约束的 plan 不得提交。
- **FR-015**：软目标至少可表达 semantic/match strength、coverage、候选质量、usage/newness、镜头/来源/人物/场景多样性、连续性、动作与声音边界、画幅可用性、节奏和 Technique Card 适配。
- **FR-016**：系统必须避免没有创作理由的精确 span 重复和近重复；用户明确的 repetition motif、recap 或素材稀缺可成为有来源例外。
- **FR-017**：同一 candidate 分配给多个 Beat、相邻 spans 合并、跨 Beat 长镜头或空槽必须有明确语义；不能依赖 UI 偶然布局解释。
- **FR-018**：求解结果必须包含目标/约束摘要、selected assignments、rejected alternatives、global trade-offs、gaps、预算、solver/compiler revision 和 deterministic validation。
- **FR-019**：系统必须返回至少一个首选 Coverage Plan；整体 alternatives 只有在真实展示不同创意方向或权衡时提供，不为凑数量生成近重复版本。
- **FR-020**：求解超时、无可行解或能力不足时必须保留已证明的部分结果和明确冲突，不退回违反硬约束的“最好努力”计划。

#### 时间、声音与剪辑边界

- **FR-021**：当可用时，assignment 必须联合 speech/ASR boundary、pause/breath、shot boundary、motion/action peak、music beat、source audio 和 Technique Card conditions；每种信号有来源与置信。
- **FR-022**：停顿/气口不得按单一阈值全部删除；语义、情绪、呼吸自然度与用户节奏偏好必须参与，并保留安全 handles。
- **FR-023**：视觉与音频 source mapping 必须可分别表达，以支持 J/L cut、保留原声、ducking 和 voice-over；没有 typed audio capability 时明确降级。
- **FR-024**：切点冲突必须按用户意图、语义完整、可听性、动作连续性、Technique Set 和装饰性卡点的可解释优先级处理。
- **FR-025**：Assignment 只能生成 Core 支持的 typed Timeline proposal；精确 source range、duration、transition handle 和音频范围在提交前验证。

#### Gap 与替代层级

- **FR-026**：Coverage 不足不得用无关低分候选填充；Gap 必须说明对应 Beat、缺失 need、已查范围、未知覆盖和冲突。
- **FR-027**：默认替代顺序应覆盖：已有直接素材 → 相关/情绪/反应/环境素材 → 保留 A-roll/原声 → 已有图片/文字/简单图形 → 调整文稿/结构 → 可选补拍；系统可按 Blueprint 调整但必须解释。
- **FR-028**：补拍不是默认主路径或成功门槛；用户可完全忽略补拍并继续完成视频。
- **FR-029**：intentional gap、黑场、留白、纯字幕或长镜头是合法创作选择，不能只因没有素材 assignment 判定失败。

#### Revision、协作与可解释性

- **FR-030**：Coverage Plan 必须固定 Blueprint、script/voice、Wiki/analysis、Creator、Technique Set、candidate query 与 capability revisions。
- **FR-031**：用户锁定的 Beat/assignment/Timeline span 在局部重算中保持不变；若新硬约束使其不可行，系统返回显式 conflict，不静默解锁。
- **FR-032**：script edit、candidate reject、source unavailable、Technique change 或 manual Timeline edit 必须产生影响分析；只重算必要子图，并输出 before/after diff。
- **FR-033**：current Timeline 是后续 Agent 修改的一等输入；旧 Coverage Plan 只能提议 reconciliation，不能覆盖手工修改。
- **FR-034**：一键 first cut 和逐步确认必须使用同一 Coverage Plan contract、validation 和 history；前者只减少交互 checkpoint，不减少证据。
- **FR-035**：每个最终 Timeline clip 必须能追踪到 Beat/Need、match evidence、assignment、Technique Card（若有）和 source span。
- **FR-036**：跨 Agent 恢复必须从结构化 Plan、locks、gaps、open decisions 和 Timeline diff 完成，不依赖旧聊天 transcript。

#### 评测与治理

- **FR-037**：Graph 只是逻辑 contract；首阶段不得以图数据库、端到端训练或云求解器为成立条件。
- **FR-038**：必须保存逐句 baseline、global plan、rendered preview 与人工 edits 的对照工件，支持同素材成对盲评。
- **FR-039**：离线评测必须分桶报告 factual、depiction、mood、continuity、usage、partial-index 和 no-answer，不能用单一总体分掩盖事实错误。
- **FR-040**：任何自动权重调整必须在 holdout 上验证且版本化；一次用户项目的反馈不能直接改 current global policy。
- **FR-041**：项目文本、OCR、ASR 和网络参考均为不可信数据，不能改变 hard constraints、工具权限或 validator。

### Key Entities

- **Script Beat**：文稿/口播中的稳定语义与时间单位。
- **Coverage Need**：一个 Beat 对事实、画面、情绪、连续性或留白的明确需求。
- **Media Candidate**：带精确 evidence、可用性和技术条件的图片或视频 span。
- **Match Evidence Edge**：Beat/Need 与 Candidate 间有类型、来源和不确定性的关系。
- **Temporal Slot**：Timeline 中该 need 可占用的目标时间窗口。
- **Assignment**：Candidate 到 Beat/Slot 的选择及其 trim/audio/role 计划。
- **Hard Constraint**：违反即不可提交的用户、事实、安全、权限或 source 条件。
- **Soft Objective**：可权衡的相关性、连续性、多样性、质量、usage、节奏和风格目标。
- **Coverage Plan Revision**：完整 assignments、alternatives、gaps、bindings 和 validation 的不可变快照。
- **Assignment Lock/Rejection**：用户对当前项目选择的局部约束。
- **Coverage Gap**：不能由当前证据安全满足的 Need 及其替代路径。
- **Impact Set**：一次变化后必须重新评估的最小 beats/edges/assignments 集。

## Success Criteria

### Measurable Outcomes

- **SC-001**：在至少 100 个、每个 8–20 Beats 的经许可真实/合成项目上，完整初剪的成对盲选胜率相对 independent top-1、independent top-k+人工去重和 greedy diversity 中最强 baseline 提升至少 15 个百分点；95% paired CI 下界大于 0。
- **SC-002**：相对最强 baseline，用户从 first preview 到 accepted rough cut 的中位 replace/move/trim 操作数降低至少 35%，主动编辑时间降低至少 30%。
- **SC-003**：must include/exclude、permission、source range、locked assignment 和 factual-support hard constraint 违反率为 0。
- **SC-004**：随机审计至少 500 个最终 clips，其 Beat/Need/match type/evidence/source span trace 完整率 100%，关系类型人工准确率 ≥95%。
- **SC-005**：同一精确 span 的无理由重复率相对逐句 baseline 降低至少 60%；用户明确 repetition motif 的保留率 100%。
- **SC-006**：视频候选 `R@5@IoU≥0.5` 不低于 ML-010 最强 baseline 2 个百分点以上；全局收益不得以牺牲精确时间召回换取。
- **SC-007**：在缺失素材测试中，无关候选填充率低于 2%，not-indexed 与 no-evidence 分类准确率 ≥98%，替代/gap 说明人工可行动率 ≥90%。
- **SC-008**：对已标注 speech/shot/action boundary 的 assignment，首版 cut point 中位误差相对固定时长 baseline 降低至少 30%，用户手工 trim 次数降低至少 25%。
- **SC-009**：锁定项在 1,000 次局部 script/candidate/technique 修改后的静默变化次数为 0；不受影响 Timeline spans 的 canonical digest 保持率 100%。
- **SC-010**：一键与逐步路径的 plan/evidence/validation 完整率均为 100%；一键路径不得产生无 evidence clip。
- **SC-011**：同一 pinned inputs、policy 和 solver/compiler revision 重放，Coverage Plan canonical digest 一致率 100%；超时/无解均有稳定结果类别。
- **SC-012**：完整 global planning 的 p95 时间和资源预算由 Spec 004 在目标硬件冻结；相对最强 baseline 超过两倍时，必须证明主动编辑总时间仍净下降，否则不晋级默认。

## Baselines and Experiment Protocol

### 必须公平比较的路径

1. 每个 Beat 独立 lexical/semantic top-1。
2. 每个 Beat 独立 typed hybrid top-k，由固定规则去重。
3. Greedy：按 Beat 顺序选择当前最高分且做简单 diversity penalty。
4. Global Footage Assignment：相同候选池、相同 evidence、相同 Agent/model/时间预算。

### 必须冻结的条件

- 同一 Library、Script/voice、analysis outputs、Creator context、Technique Set 与 render capability。
- 不允许 Global 路径额外读取更多帧或使用更强模型；额外 refinement 单独计入成本。
- 盲评看完整 preview，不只看 pair score；评委不知道生成路径。
- 同时报告质量、事实错误、重复、连续性、gap、首版延迟、总编辑时间、operations 与 payload 成本。

### 推荐分桶

- 口播 + 私人 B-roll；Vlog；图片故事；有/无 voice track；素材充足/稀缺。
- factual、action、reaction、establishing、metaphor、mood、A-roll 与 intentional gap。
- 未用优先、允许复用、部分 used long video、成片误导候选。
- 索引完整、部分索引、source offline 和 capability-limited。

## Rollback and Degradation

- 首阶段只生成 Coverage Plan shadow，不提交 Timeline；与逐句 baseline 并排审查。
- Global solver 无解/超时时回退“已验证的 partial plan + gaps”，不回退违反硬约束的拼接结果。
- 精细 speech/motion 信号不可用时使用 shot/semantic 安全窗口并标记降级；不假装做过气口判断。
- Graph representation 没有证明局部更新/解释收益时，可退回等价 typed tables；项目 contract 保持。
- 用户随时可锁定当前 Timeline，关闭自动 replan，继续手工或逐段检索。

## Out of Scope

- 不在本规范选择 ILP、beam search、graph neural network、LLM planner 或具体排序公式。
- 不保证任何文稿都必须被 B-roll 填满。
- 不自动改写用户立场或把补拍变成强制步骤。
- 不生成、移动、删除或上传原始媒体。
- 不实现完整专业 NLE、社交平台发布或自动版权清关。
- 不用单一“审美分”取代用户创意选择。

## Assumptions

- ML-009/010/014 提供带类型、精确时间和 coverage 的候选 evidence。
- ML-012 提供可验证 script claim/evidence contract；ML-016 提供 Technique conditions。
- ML-015 提供 Blueprint、operation history、cross-Agent resume 与可视化工作台。
- 第一阶段可以在现有 Timeline typed operations 上验证，不必先替换成 OpenCut。
- 用户希望快速得到一个不错版本，但始终可以锁定、替换和局部重算。

## Authorization Boundary

用户已授权按纵向主链逐个实施必要子切片。当前 [ML-018-A1](slices/018-a1-canonical-coverage-plan/spec.md) 已实现确定性的 canonical Coverage baseline：只从 exact Blueprint script/material hints 与 Core-verified evidence 物化 Beat、assignment、alternative 和 honest gap。

该 staged authorization 不把父规范整体升级为已实现：语义/音频分析、模型调用、完整 candidate generation、global solver、locks/rejections、局部重算、效果优越性 benchmark 与任何 Timeline mutation 仍未由 ML-018-A1 实现或验证；Timeline 只能由独立的 ML-015-B2B typed lowerer 切片取得写权限。图数据库仍不获授权，原始媒体不得移动、删除、覆盖或上传。
