# Feature Specification: Intent Compiler & Evidence Federation

- Feature ID：`ML-009`
- 创建日期：2026-08-20
- 状态：`PROPOSED / EXPERIMENT`
- 实施授权：`NONE`
- 组合决策：[MUST-STAGED / 009-A Core](../implementation-decisions-2026-08-20.md)；先统一 deterministic intent、honest empty 与 evidence DTO，融合/LLM 慢路径继续实验
- 规范类型：Spec Kit 风格的产品—架构混合规范
- 证据截止：2026-08-20
- 优先级：P1 能力跃迁
- 依赖：[Spec 004](../004-evidence-backed-retrieval-privacy-benchmark/spec.md)、[Spec 007](../007-local-capability-boundary/spec.md)、[Spec 008](../008-unified-media-memory-kernel/spec.md)
- 主要外部基准：[PhotoBench，KDD 2026 Datasets & Benchmarks](https://doi.org/10.1145/3770855.3817511)，[arXiv 版本](https://arxiv.org/abs/2603.01493)

## Overview

MemoLens 应把自然语言查询编译成可验证的意图结构，再让视觉、文本、时间、地点、人物、关系、事件和质量等证据源独立召回。系统用显式约束和可校准融合形成结果，保留每个候选被加入、降权或排除的原因，并在没有足够证据时拒答或询问一个高价值澄清问题。

这不是“用 Agent 代替搜索”。Planner 只能提出受 contract 约束的 query plan；确定性约束、候选集合、置信与 provenance 由检索系统验证。Embedding 负责模糊语义，不负责精确日期、人物身份、关系、否定或权限。

## Hypothesis and Falsification

### Hypothesis

私人媒体查询的主要损失来自异构约束没有被正确分解与融合，而不只是视觉模型不够强。类型化 intent、source-specific retrieval、可靠性加权和主动拒答能显著改善多来源查询，同时保持简单查询低延迟。

### Falsification / Kill Criteria

- 在固定 PhotoBench/MemoLensBench 上，primary nDCG@10 相对最佳简单 baseline 的提升低于 5% 或 95% paired bootstrap CI 包含零，则终止该策略；提升为 5% 至 20% 时只允许继续 shadow；达到 20% 且所有 guardrail 通过才可 promote。
- 多来源融合退化没有改善，或错误硬交集继续删除 ground truth，则取消通用 planner，退回固定 query templates。
- Overall p95 超过最佳 baseline 两倍且困难查询 Recall 提升不足 5 个百分点，则取消慢路径。
- Zero-GT rejection 或 answerable-query coverage 任一门槛失败，则禁止用“记忆理解”作为公开能力声明；全部拒答不能获得通过结果。

## User Scenarios & Testing

### User Story 1：用户用生活语言查找多条件记忆（Priority: P1）

用户可以说“找去年去机场前和父母吃晚饭的照片，不要自拍”，系统分别理解事件、相对时间、人物关系、视觉语义和排除条件，而不是只匹配几个词。

**Why this priority**：这类查询正是私人相册区别于网页图文检索的核心。

**Independent Test**：使用同时包含视觉、时间、人物/关系和否定 ground truth 的固定 query，验证计划结构、各源候选和最终结果。

**Acceptance Scenarios**：

1. **Given** query 同时包含视觉、相对时间和人物关系，**When** 编译，**Then** 每个条件成为独立、可检查的 clause，并标记 hard/soft 与不确定性。
2. **Given** 一个来源缺失或置信低，**When** 融合，**Then** 系统不会盲目硬交集删除其他高证据候选，而是按预声明规则降权、澄清或拒答。
3. **Given** 明确“不要自拍”，**When** 返回结果，**Then** 每个结果都满足可验证排除约束，或明确说明该约束无法验证。
4. **Given** 查询包含“去机场前”，**When** 系统已有相关事件时间，**Then** 时间窗口来源可追溯到事件证据，而不是由模型凭空生成。

### User Story 2：用户得到诚实空结果与澄清（Priority: P1）

当用户的记忆有误、条件互相冲突或证据不足时，MemoLens 不返回无关“差不多”素材。系统明确区分未找到、未索引、无权限、条件冲突和需要澄清。

**Why this priority**：错误肯定用户的私人记忆会损害信任，也会把不相关素材带入创作。

**Independent Test**：使用无 ground truth、冲突日期、同名人物、缺失来源和索引不完整查询，检查 abstention 与 clarification。

**Acceptance Scenarios**：

1. **Given** 无正确结果查询，**When** 所有候选证据低于门槛，**Then** 返回可解释空结果，不补齐 top-k。
2. **Given** “周五”和确切日期冲突，**When** 编译，**Then** 系统指出冲突并请求澄清，不静默任选一个。
3. **Given** 两个人使用同一昵称，**When** 人物约束影响结果，**Then** 系统询问最能减少歧义的一个问题或展示明确分组。
4. **Given** 所需来源未完成分析，**When** 查询，**Then** 响应区分“没有证据”和“证据尚未建立”。

### User Story 3：用户能理解为什么选中与为什么没选中（Priority: P1）

用户查看结果时，可以看到主要证据、满足的条件、未验证条件和排除原因；用户选定某个被系统排除的素材时，系统能解释冲突并允许显式覆盖。

**Why this priority**：可解释裁剪是解决 source fusion paradox 和创作可信度的关键。

**Independent Test**：选择 ground truth、近似候选和被错误来源排除的候选，检查 evidence/exclusion trace 是否完整且不暴露敏感内部数据。

**Acceptance Scenarios**：

1. **Given** 一个最终结果，**When** 用户展开原因，**Then** 至少显示命中的 clause、证据范围、source freshness 和置信状态。
2. **Given** 一个高视觉相似候选被时间条件排除，**When** 查看原因，**Then** 显示可验证时间证据和排除规则。
3. **Given** 用户显式选择违反 soft preference 的素材，**When** 加入项目，**Then** 系统允许覆盖并记录用户决定。
4. **Given** 候选违反 hard safety/permission 条件，**When** 用户尝试覆盖，**Then** 仍拒绝并给出可操作说明。

### User Story 4：简单查询快速，困难查询才深入回忆（Priority: P1）

用户搜索“海边日落”时快速得到结果；只有多跳、冲突或低置信查询才扩展事件邻居、改写查询或定向调用昂贵复核。

**Why this priority**：桌面产品不能让每次搜索都承担 Agent/VLM 成本。

**Independent Test**：将固定 query 标为 simple/hard，观察路由信号、慢路径触发率、延迟和质量。

**Acceptance Scenarios**：

1. **Given** 单一视觉查询且 top candidates 分离充分，**When** 搜索，**Then** 使用快路径并返回。
2. **Given** 多来源冲突或候选分数不确定，**When** 搜索，**Then** 慢路径只扩展相关 source 和候选，不扫描全部原媒体。
3. **Given** 慢路径达到证据预算，**When** 仍不确定，**Then** 停止并拒答/澄清，不无限迭代。
4. **Given** 离线 profile，**When** 慢路径需要不可用远端模型，**Then** 使用本地降级并标记能力差异，不暗中外发。

### User Story 5：维护者可替换检索器而不改变查询语义（Priority: P2）

维护者可以比较不同 sparse、dense、metadata、event 或 reranker adapter；query clause、result evidence 和指标保持稳定。

**Why this priority**：创新应来自可验证的检索合同，不应被某个模型或向量库锁定。

**Independent Test**：在同一 query plan 上替换单个 retriever，其他 source 固定，比较候选、融合和结果。

**Acceptance Scenarios**：

1. **Given** 固定 query plan，**When** 替换 visual retriever，**Then** metadata/person/time clauses 及其证据不改变。
2. **Given** 新 retriever 未提供所需 provenance，**When** 注册，**Then** contract test 拒绝进入默认路径。
3. **Given** adapter 失败，**When** 查询仍可用其他来源回答，**Then** 响应明确降级，不把失败来源当作空集合做硬交集。

## Edge Cases

- “以前”“最近”“毕业后”“去机场前”等相对或事件锚定时间。
- 时区、夏令时、相机时间错误和导入时间与拍摄时间混淆。
- 人物昵称、关系变化、同名、未确认身份或敏感身份禁用。
- 地点别名、GPS 缺失、地点推断与 EXIF 冲突。
- 否定范围，例如“不要自拍里的海边”与“海边，但不要自拍”。
- One-to-many query、burst、near-duplicate 和代表性选择。
- 一个 clause 只有视觉 proxy，例如“生日”可见蛋糕但实际事件 metadata 不足。
- Query language 与 caption/ASR/OCR language 不同。
- Planner 生成未注册 clause、循环依赖或超预算计划。
- Source 返回分数不可比较、缺 provenance、过期或 partial analysis。
- Hard filter 高精度但低 recall，错误交集删除正确候选。
- 用户要求按情绪、叙事价值或“像我会发的”排序，属于主观 policy 而非事实约束。
- Asset 已 archive、source unavailable 或权限撤销，但旧项目仍引用。
- Query 在执行期间 library generation 切换。

## Requirements

### Functional Requirements

- **FR-001**：系统必须把 query 编译成版本化 intent representation，并保留原 query、语言、时间上下文和编译 provenance。
- **FR-002**：Intent 必须能分别表达 visual、text/OCR/ASR、time、place、person、relationship、event、media type、quality、negative、cardinality 和 ordering clause。
- **FR-003**：每个 clause 必须标记 hard/soft、source requirements、confidence/ambiguity、scope 和可验证条件。
- **FR-004**：不确定且显著影响结果的 clause 必须触发澄清、候选分支或明确降级，不得假装确定。
- **FR-005**：Planner 只能产生已注册、可验证、受预算约束的 plan；未知 operation、无限循环或越权 source 必须被拒绝。
- **FR-006**：视觉、文本、metadata、人物、关系、事件和其他 retriever 必须独立返回候选、source-specific score、evidence 和 freshness。
- **FR-007**：不同 source 的原始 score 不得被假设可直接比较；融合规则必须版本化并在 benchmark 中校准。
- **FR-008**：精确 metadata/permission 条件必须使用可验证 evidence，不得仅由 embedding 相似度满足。
- **FR-009**：Embedding 不得作为人物身份、精确日期、权限或 hard negation 的唯一证据。
- **FR-010**：Fusion 必须区分 hard exclusion、soft penalty、unknown 和 source unavailable。
- **FR-011**：低可靠性或缺失 source 不得默认作为空集合参与硬交集。
- **FR-012**：每个候选必须保留进入、合并、重排、降权和排除 trace。
- **FR-013**：Final result 必须引用稳定 asset/segment identity、主要证据、满足/未验证 clause 和结果 confidence state。
- **FR-014**：系统必须支持 one-to-many ground truth 和多样性选择，不能用 near-duplicate 填满结果。
- **FR-015**：系统必须定义 honest empty state；零证据候选不得仅为填满 top-k 而返回。
- **FR-016**：系统必须区分 no evidence、not indexed、source unavailable、permission denied、conflicting constraints 和 ambiguous query。
- **FR-017**：系统可以提出澄清，但每轮只选择预期信息增益最高的最少问题，并有次数预算。
- **FR-018**：Query execution 必须有 source、候选、模型调用、迭代、延迟和隐私预算。
- **FR-019**：Fast/slow routing 必须基于在 Spec 004 中可独立校准、可重放的观测信号，而非模型自报 confidence；具体路由信号在后续技术计划和消融实验中选择。
- **FR-020**：慢路径只能处理受限候选或相关事件邻居，不能默认重新分析整个 library。
- **FR-021**：达到预算仍不确定时必须停止并拒答/澄清。
- **FR-022**：离线模式不得触发未授权远端 planner、embedding、reranker 或 VLM。
- **FR-023**：Retriever/fusion/model 的版本变化必须进入 run provenance，并受 Spec 004 regression gate。
- **FR-024**：Photo、Video、Atlas、Codex 和 Photon 必须消费相同 intent/result evidence contract，允许 surface-specific presentation，不允许语义分叉。
- **FR-025**：用户显式选择可覆盖 soft preference，且覆盖记录成为 project provenance；hard safety/permission 不可覆盖。
- **FR-026**：用户 correction 必须作为 immutable feedback event 进入 Spec 011 或评测，不得直接原地改写历史 query/result。
- **FR-027**：所有结果型质量声明必须按 source count、query type、language 和 no-answer 分桶报告。
- **FR-028**：系统必须提供 replayable query plan，使失败能在冻结 projection generation 上重现。
- **FR-029**：Event clause 是本规范的稳定 contract，但在 Spec 010 event adapter 验证前必须返回 `unsupported/not-indexed`，不得以视觉 proxy 假装满足；默认路径验收必须区分 `009 core` 与 `009 + 010 event adapter`。
- **FR-030**：Abstention 评测必须同时覆盖 Zero-GT rejection precision/recall/F1、answerable-query false-abstention/coverage、selective risk-coverage curve 和固定 coverage 下的 ranking quality。

### Key Entities

- **Intent**：原 query 编译后的版本化意图。
- **Clause**：可独立验证的视觉、时间、人物、关系、事件、否定或排序条件。
- **Query Plan**：受预算和 policy 约束的 source 调用与融合步骤。
- **Retriever**：对一种证据源产生候选和 provenance 的适配器。
- **Candidate Evidence**：asset/segment、source、score、span、freshness 和 clause match。
- **Candidate Set**：某 retriever 在某 generation 上的有序集合。
- **Fusion Trace**：候选如何合并、降权、排除和重排的可重放记录。
- **Constraint Conflict**：clause 之间或用户选择与 hard/soft policy 之间的冲突。
- **Abstention**：没有足够证据时的显式终态与原因。
- **Clarification**：为减少关键歧义而提出的有限问题与用户回答。
- **Retrieval Budget**：source、候选、时间、模型、隐私与迭代上限。

## Success Criteria

### Measurable Outcomes

- **SC-001**：在冻结 PhotoBench/MemoLensBench 上，预注册 primary metric `nDCG@10` 相对最佳现状 baseline 提升至少 20%，三次固定运行的 paired bootstrap 95% CI 下界大于 0；不允许用其他创作或检索指标替换 primary endpoint。
- **SC-002**：对同一 query 做 paired source ablation，分别报告各源 Recall@20、oracle-union Recall@20、fusion 前后 Recall@20、错误 hard-intersection 删除 ground truth 比例和 source-count-matched baseline；fusion 后 Recall@20 不低于 oracle union 5 个百分点以上，可确定 hard clause 导致的错误 ground-truth 删除率为 0。
- **SC-003**：Zero-GT rejection precision、recall、F1 均至少 95%；answerable query coverage 至少 95%、false-abstention 不超过 5%；同时报告 selective risk-coverage curve，并在 95% coverage 下保持 SC-001 的 ranking guardrail；no-evidence 与 not-indexed 原因区分准确率至少 95%。
- **SC-004**：在回答的 query 上，可确定时间、媒体类型、权限和明确否定 hard constraints precision 为 100%；对应 constraint recall 至少 95%，answerable-query answered coverage 至少 90%，禁止靠全部拒答满足 precision。
- **SC-005**：随机抽查 100 个结果，进入/排除 trace 和主要 evidence 覆盖率为 100%。
- **SC-006**：在 Spec 004 冻结、且 simple/hard 比例固定的 query manifest 上，至少 95% simple query 停留 fast path；hard query 的慢路径触发率单独报告，不能通过改变 benchmark 组成满足 overall 比例。
- **SC-007**：10k 资产热 fast-path 查询 p95 不超过 500ms；需要慢路径时 800ms 内返回可用 fast candidates 或明确 pending/abstention，local slow-path completion p95 不超过 5 秒，remote-provider track p95 不超过 30 秒并单独报告调用数、字节和成本；超时不占用交互 HTTP 请求。
- **SC-008**：困难 query Recall@20 相对 fast-only baseline 提升至少 15 个百分点。
- **SC-009**：以“最终返回集合满足全部声明 hard clause”为 confidence target，在不少于 500 个独立 query 上使用 10 个 equal-mass bins 计算 ECE，值不超过 0.05；达不到时只展示分档状态，不展示误导性精确概率。
- **SC-010**：离线 benchmark 的非 loopback 请求为 0；任何远端复核均有 Spec 007 disclosure。
- **SC-011**：Photo、Video、Atlas、Codex 和 Photon 对同一 fixture 的 clause 与 result identity 一致率为 100%。
- **SC-012**：三次固定运行均达到 primary、constraint、abstention、privacy 和 latency guardrail 才允许进入默认路径；提升低于 5% 或 CI 包含零则 kill，5% 至 20% 保持 shadow，达到 20% 且 guardrail 全过才 promote。

## Baseline and Experiment Protocol

至少比较：

- 当前 mixed lexical fallback。
- 当前 legacy photo hybrid/MMR。
- 固定 sparse/metadata baseline。
- 固定 dense visual/text baseline。
- 无 Agent 的 deterministic typed plan。
- 受约束 planner + evidence fusion。

每次只改变一个主要变量；planner-only 使用预计算 source evidence，retriever-only 使用固定 plan。报告候选 recall、oracle-union、fusion loss、错误交集、final rank、abstention、coverage 和 cost，不能只报告端到端分数。单/双/三来源结果使用同一 query 的 paired ablation 或 source-count-matched query，不把不同难度 bucket 的差值解释为融合损失。

## Rollback and Degradation

- 默认路径切换前运行 shadow compare，不影响用户结果。
- Planner 不可用时使用 deterministic query templates，不退回自由文本工具调用。
- 某 source unavailable 时标记 unknown/degraded，不以空集合做硬交集。
- 慢路径超预算时返回 fast result 加不确定性，或诚实拒答。
- 新 fusion/model 回归时切回已验证 version，保留 query/fusion trace 供分析。

## Out of Scope

- 不选择具体 LLM、embedding model、向量引擎、reranker 或图数据库。
- 不在本规范中学习长期 Creator preference。
- 不用 retrieval confidence 证明人物身份或事实真实性。
- 不承诺回答库中没有证据的问题。
- 不要求所有查询都进入 Agent 或慢路径。

## Assumptions

- Spec 008 提供稳定 asset、segment、projection generation 和 model space；Event 只在 Spec 010 提供验证 adapter 后可作为已索引 evidence。
- 精确人物关系只有在用户确认或明确数据源存在时可作为 hard evidence。
- 部分情绪和叙事词只能作为 soft semantic preference。
- PhotoBench 是高相关诊断集，但必须与合成和经同意项目数据互补。
- 性能阈值会在 Spec 004 固定硬件 profile 后锁定。

## Dependencies

- Spec 004 提供数据、baseline、分桶、指标和复现工件。
- Spec 007 限制 source、provider 与 surface capability。
- Spec 008 提供统一 canonical identity、projection 和 operation lifecycle。
- Spec 010 可作为 event/temporal retriever；Spec 011 可提供显式 creator policy，但不能改变 hard fact semantics。
