# Feature Specification: Agent-Navigable Media Wiki

- Feature ID：`ML-014`
- 创建日期：2026-08-22
- 状态：`PROPOSED / EXPERIMENT`
- 实施状态：[ML-014-A0 Live Agent Media Wiki Read Surface](slices/014-a0-live-read-surface/spec.md) 已实现并验证；本规范其余范围仍未授权
- 输入决策：[43 问产品对齐记录](../product-decisions-43-questions-2026-08-22.md)
- 规范类型：Spec Kit 风格的产品规范；候选机制需在实施前移入 `plan.md` / ADR
- 优先级：P0 下一代素材理解与 Agent 检索底座
- 依赖：[Spec 008](../008-unified-media-memory-kernel/spec.md)、[Spec 009](../009-intent-evidence-retrieval/spec.md)、[Spec 010](../010-hierarchical-temporal-memory/spec.md)
- 相关调研：[Agent 可导航媒体 Wiki 调研](../../agent-media-wiki-research-2026-08-22.md)

## Overview

MemoLens 需要把一个本地 Library 逐步编译为 Agent 可搜索、可浏览、可沿关系展开、可回到原始时间证据的“活素材 Wiki”。它不是把图片说明和视频摘要切成文本块后做一次向量 top-k，也不是让 Agent 每次从头扫描整个文件夹。

Wiki 的职责是让 Codex、Claude 或其他 Agent 先看到一个小而清楚的素材地图，再按创作问题逐层钻取：

```text
Library 地图
  → 事件 / 主题 / 人物候选 / 项目 / 作品页面
  → Asset 页面
  → 精确视频 span、帧、字幕或音频证据
  → 必要时请求更密集的定向分析
```

原始媒体和 Canonical Media Ledger 仍是事实权威；Wiki 是版本化、可重建的知识投影。模型生成的场景、人物、动作、情绪、叙事和技巧判断只是有来源的 hypothesis。用户确认、确定性媒体事实和导出使用记录必须与模型假设分级。

本规范吸收三类已验证思想，但不直接复制任何项目：

- OriNodes-v4 的证据优先、分层知识、精确钻取、增量 generation 和快照治理；
- Google Code Wiki / Open Knowledge Format 的持续更新、渐进披露、可读可移植页面、provenance、trust、freshness 和 lifecycle；
- Agent-native retrieval 研究的 search → read → follow → verify → stop 组合式检索。

## Hypothesis and Falsification

### Hypothesis

对于同时包含语义、时间、事件、使用状态和创作约束的问题，分层 Wiki 页面、类型化关系、证据钻取和 Agent 组合式导航，能够比当前 lexical mixed search、纯 dense RAG 和一次性 hybrid top-k 更快找到可直接用于 Timeline 的正确素材组合，并减少错误匹配与无根据总结。

### Falsification / Kill Criteria

满足任一条件时，Wiki 不得晋级为默认检索主路径，只保留为可读浏览投影：

1. 在相同分析产物和模型预算下，复杂创作检索的任务成功率相对最强非 Wiki baseline 提升不足 10 个百分点。
2. 简单单条件查询的 p95 延迟超过最强 baseline 两倍，且路由无法让至少 95% 简单查询停留在快路径。
3. Wiki 编译导致原始细节丢失，使时间段定位 Recall 或 evidence correctness 下降超过 3 个百分点。
4. 增量维护在 100k 资产规模仍要求正常路径全库扫描、全图重建或 N×N 结构。
5. 不能稳定区分确定性事实、模型假设和用户确认，或旧项目会随 Wiki 更新静默漂移。
6. 相同权限下，Wiki 路径比现有检索增加任何未记录的原始媒体外发。

## User Scenarios & Testing

### User Story 1：一个文件夹逐步变成可创作的素材地图（Priority: P1）

创作者只选择一个包含照片和视频的 Library。系统先让最早可用的素材进入搜索，再在后台逐步形成资产、时间段、事件候选、主题、历史项目与使用状态页面。素材很多时，用户能看见范围、进度、预计时间和仍未覆盖的部分。

**Why this priority**：冷启动必须简单，且全库深度分析不能阻塞第一条视频。

**Independent Test**：使用 1k、10k、100k 混合媒体库，在索引任一阶段开始搜索和创建项目，检查结果覆盖范围、缺口说明、退出恢复和最终一致性。

**Acceptance Scenarios**：

1. **Given** 新 Library 尚未分析，**When** 用户选择目录，**Then** 系统先建立稳定资产身份和可见进度，不要求用户分类或填写标签。
2. **Given** 只有部分素材形成 Wiki，**When** 用户搜索，**Then** 结果明确标注已搜索范围与未完成范围，不把“没索引”说成“没有素材”。
3. **Given** 当前项目只引用一个子目录，**When** 后台仍有大量历史素材，**Then** 当前范围优先进入可用状态，全库任务继续可暂停、恢复。
4. **Given** 编译进程在任一阶段退出，**When** 应用重启，**Then** 上一个完整 Wiki generation 仍可用，半成品不成为 current。

### User Story 2：从文稿的一句话找到私人素材的具体时刻（Priority: P1）

创作者给出一句口播、一个 script block 或表达意图。Agent 返回若干可替换候选，每个候选是图片或视频的精确时间范围，说明为什么匹配、用过哪些秒、还有哪些未使用画面，以及证据是否足以支持这句文稿。

**Why this priority**：文稿与私人 B-roll 的逐句匹配是用户明确指出的高频痛点。

**Independent Test**：对带人工标注的 script-block → span 数据，比较文件级摘要、flat chunk RAG、typed hybrid 和 Wiki navigation 的可用候选率、时间范围准确率与完成时间。

**Acceptance Scenarios**：

1. **Given** 一条长视频只有 4 秒画面符合文稿，**When** Agent 搜索，**Then** 候选直接引用该 `[start_ms, end_ms)`，不是只返回整文件。
2. **Given** 最相似片段已在另一个成片中使用，**When** 查询要求“尽量不要重复”，**Then** 结果解释使用范围并优先给出未用候选；用户仍可选择复用。
3. **Given** 文稿陈述具体事实而素材只表达相似情绪，**When** 返回候选，**Then** 系统区分“事实支持”和“氛围适配”，不把后者伪装成证据。
4. **Given** 没有足够候选，**When** Agent 完成检索，**Then** 返回明确 gap、已检查范围和最有价值的下一步，而不是填满固定结果数。

### User Story 3：Agent 能组合搜索、阅读、沿链接展开和定向复核（Priority: P1）

面对“找出那次海边旅行里没用过、适合放在这句转折之后的安静镜头”这类多条件问题，Agent 可以先定位旅行事件，再查看未使用关系、候选镜头和节奏证据，只在信息不足时请求少量帧或短片段复核。

**Why this priority**：高级素材搜索需要随着中间发现改变检索计划，不能依赖一次性 top-k。

**Independent Test**：构造需要 2–4 步关系导航的任务，固定 Agent、模型、预算和底层候选信号，比较一次性 retrieval 与组合式 traversal。

**Acceptance Scenarios**：

1. **Given** 查询需要 event → asset → span → usage 四类信息，**When** Agent 执行，**Then** 每一步都有结构化工具结果、预算消耗和命中路径。
2. **Given** 初始页面证据已经充分，**When** Agent 判断可回答，**Then** 不读取原视频或触发额外模型分析。
3. **Given** 关系链出现冲突或断链，**When** Agent 展开，**Then** 返回 contradiction/dangling/stale 状态，不自行补全关系。
4. **Given** 达到 hop、节点、帧、字符或模型预算，**When** 证据仍不足，**Then** Agent 停止并报告不确定性，不退化成全库扫描。

### User Story 4：模型分析可以演进，但历史和用户修正不会被覆盖（Priority: P1）

新模型可以产生更好的描述或关系；用户也能纠正事件、人物候选、时间、主题或素材适用性。系统保存新 revision、来源和 supersession，不原地重写旧 observation，也不让低权限模型覆盖用户确认。

**Why this priority**：一个长期素材库会跨越模型、提示词和用户认知的多次变化。

**Independent Test**：对同一素材依次运行两个分析版本并插入用户修正，重建 Wiki、切换 current head、回退后核对所有引用。

**Acceptance Scenarios**：

1. **Given** 新模型与旧模型对场景判断不同，**When** 新 generation 激活，**Then** 两个 observation 都保留，current resolution 与理由可见。
2. **Given** 用户确认某关系错误，**When** 以后重新分析，**Then** 自动 hypothesis 不得静默重新成为确认事实。
3. **Given** 一页引用的原始 source 已改变或不可用，**When** 打开页面，**Then** 页面被标为 stale/source unavailable，历史项目引用仍可解释。
4. **Given** Wiki 编译出现重复断链或错误归因，**When** QA 发现，**Then** 问题进入可审计 compiler issue ledger；自动修复只能生成新 revision。

### User Story 5：项目冻结自己的 Active Creation View（Priority: P1）

创作者建立 first cut 时，项目固定它使用的 Wiki generation、Creator Memory revision、Creative Blueprint revision、查询计划和 Timeline revision。以后素材库继续分析，旧项目和导出仍可重放当时的选择。

**Why this priority**：活 Wiki 不能让历史作品的素材理由和结果静默漂移。

**Independent Test**：创建项目后更新素材、模型、Wiki 和 Creator Memory，再重放旧 revision 并比较候选、Timeline 与 export manifest。

**Acceptance Scenarios**：

1. **Given** 项目已生成初剪，**When** current Wiki 更新，**Then** 项目保持固定旧 generation，除非用户显式升级。
2. **Given** 用户选择升级项目知识视图，**When** 重新检索，**Then** 系统先展示候选变化和影响，再创建新项目 revision。
3. **Given** 历史素材已移动但 hash 相同，**When** 重放项目，**Then** 稳定 asset/span 引用保持，source relink 形成显式 operation。

### User Story 6：同一 Wiki 能被不同 Agent 和人阅读（Priority: P2）

创作者可以在 MemoLens 工作台浏览素材地图，也可让 Codex、Claude 或另一个兼容 Agent 通过稳定工具读取同一知识。系统可生成可读、可移植的 Wiki 快照，但该快照不是主数据库。

**Why this priority**：Agent 无关和可解释性是用户明确要求，也是避免被单一模型平台锁定的关键。

**Independent Test**：让两个 Agent 通过相同 contract 完成固定任务；另行导出 Wiki bundle，验证人类可浏览、链接可解析、来源和 digest 完整。

**Acceptance Scenarios**：

1. **Given** 两个兼容 Agent 使用同一 pinned generation 和查询条件，**When** 搜索，**Then** 底层候选、证据 ID 和 hard constraint 结果一致；自然语言表达可不同。
2. **Given** Agent 不认识某种新页面类型，**When** 读取，**Then** 它可以按通用页面处理，而不是拒绝整个 bundle。
3. **Given** 用户导出可读 Wiki，**When** 用普通文本工具打开，**Then** 页面、索引、来源、状态和链接无需专有 SDK 即可理解。

## Edge Cases

- 同一文件有多个路径、路径移动、内容在原路径被替换、可移动磁盘暂时离线。
- 一段视频跨镜头表达同一动作；一个镜头同时适配多个主题或脚本块。
- 相同事件由多台设备拍摄且时钟不一致；EXIF、文件时间、语音内容互相冲突。
- 关键动作只在两个代表帧之间出现；粗分析遗漏细节，后续 refinement 才发现。
- 模型生成“人物身份”“第一次”“从未”等无法由覆盖范围证明的绝对结论。
- 图片/视频描述高度相似，但属于不同时间、地点或人物。
- 一个片段部分用过、部分未用；同一片段在多个导出版本使用。
- Wiki page summary 已过时，但其原 evidence 仍有效；或反之。
- Agent 在页面正文中遇到恶意提示词、字幕、OCR 或文件名注入。
- 自动编译产生循环链接、dangling link、孤儿页面、重复概念或相互矛盾的 relation。
- 全库只有部分完成大模型分析；不同 asset 使用不同模型或 prompt schema。
- Sensitive media 允许 App 本地浏览但不允许当前 Agent 打开帧。
- 两个 Agent 同时请求 refinement、写回不同结果或尝试激活 generation。
- 当前 Wiki generation 完整，但导出的人类可读 bundle 中途失败或磁盘满。

## Requirements

### Functional Requirements

#### 权威、层级与页面

- **FR-001**：原始媒体与 Canonical Media Ledger 必须是事实权威；Wiki、全文索引、向量索引、关系邻域和可视化均为可删除重建的 projection。
- **FR-002**：Wiki 必须支持至少 Library、Collection/Event/Episode、Asset、Temporal Span、Observation/Claim、Project/Work 和 Export/Usage 这些可区分知识单元；是否物理“一单元一文件”留给技术计划。
- **FR-003**：视频相关页面或结论必须能回到稳定 asset identity 与 `[start_ms, end_ms)`；图片必须回到稳定 asset identity。
- **FR-004**：每个页面必须具有稳定 identity、类型、revision、content digest、generation、生成者/方法、生成时间、状态、freshness 和来源摘要。
- **FR-005**：页面正文中的事实性 claim 必须具有稳定 evidence reference；一个笼统 page-level source 不能替代关键 claim 的精确 attribution。
- **FR-006**：Wiki 必须提供分层 index/overview，让 Agent 在读取页面正文前知道可用范围、页面类型、描述、覆盖和缺口。
- **FR-007**：高层摘要、主题和事件不能替代底层 evidence；任何高层结果必须支持受限 drilldown。
- **FR-008**：系统不得默认为每个微小时段生成 Markdown 文件。页面粒度必须受可维护性、访问频率与信息密度约束，细粒度 span 可以通过结构化 evidence view 按需展开。

#### 事实等级、关系与版本

- **FR-009**：所有 observation、claim 和 relation 必须标注 authority：至少区分 `deterministic`、`model_hypothesis`、`user_confirmed` 和 `external_reference`。
- **FR-010**：模型不得把推测的人物身份、关系、事件成员、情绪或意图直接提升为用户确认事实。
- **FR-011**：关系必须具有明确类型、起点、终点、evidence、置信/未知、analysis revision、观察时间和有效时间；纯相似关系不得冒充身份或同一事件关系。
- **FR-012**：至少支持 `span_of`、`supports`、`member_of`、`before/after/overlaps`、`depicts`、`similar_to`、`matches_script_block`、`selected_in`、`used_in`、`derived_from`、`inspired_by` 与 `supersedes/invalidates` 的语义等价关系。
- **FR-013**：自动更新只能新增 revision、relation delta 或 supersession，不得原地改写旧 observation、用户确认或历史项目引用。
- **FR-014**：每个 current Wiki view 必须由一个完整 generation 指定；新 generation 只有在其声明的 ledger watermark 全部处理并验证后才能原子激活。
- **FR-015**：Project Active Creation View 必须固定 Wiki generation、Creator Memory revision、Blueprint revision、query plan digest、Timeline revision 和所选 evidence；升级其中任一项都产生新项目 revision。

#### Agent-native 检索与证据钻取

- **FR-016**：Agent 必须能分别执行发现、搜索、打开页面、列关系、读取证据、请求受限 refinement、查看检索轨迹和报告 gap；不得只有一个不透明的“问 Wiki”接口。
- **FR-017**：检索必须支持 search-first 和 browse-first 两条路线，并能依据中间结果修改下一步，而不是预先固定一次 top-k。
- **FR-018**：检索计划必须保留 hard constraints、soft preferences、unknown、unsupported 和 exclusions；不同来源不得被错误强交集清空后再用无关结果补满。
- **FR-019**：候选生成必须能够组合全文、语义、媒体类型、时间、地点、事件、关系、质量、方向/画幅、使用状态和项目约束；任何单一 embedding 都不能承担全部真值。
- **FR-020**：关系展开必须具有 hop、节点数、时间、字符/token、帧、短片时长、模型调用和隐私预算。
- **FR-021**：每个最终候选必须返回命中路径、selection reason、满足/未知/冲突条件、evidence refs、analysis coverage、usage intervals 和仍可钻取的能力。
- **FR-022**：没有证据、尚未索引、来源不可用、权限不足、预算耗尽和约束冲突必须是不同稳定结果；系统不得用低分候选制造“看似有答案”。
- **FR-023**：定向 refinement 只能读取明确候选窗口；新增 observation 必须记录请求原因、输入 payload、Agent/model identity、输出 schema 和成本，并进入新 analysis revision。
- **FR-024**：Agent 生成的搜索词、页面正文、OCR、字幕与文件名都视为不可信数据；不得因此扩大工具权限、执行命令或改变系统规则。

#### 使用历史与创作适配

- **FR-025**：Wiki 必须呈现 exported work、使用过的精确 interval、仍未使用 interval 和引用项目；“用过”不等于从检索中永久删除。
- **FR-026**：已导出成片必须与原始素材区分，默认不作为 raw B-roll 候选，但可作为历史作品、风格参考和 provenance 被搜索。
- **FR-027**：Script block、Creative Blueprint 与素材 evidence 必须能建立可追溯匹配关系，且区分事实支持、视觉说明、情绪/节奏适配和纯装饰用途。
- **FR-028**：一次创作应能检索互补素材集合，而不只给每个句子独立最高分结果；集合选择必须解释覆盖、重复、节奏和使用历史权衡。

#### 可移植、隐私与治理

- **FR-029**：App、CLI、MCP/Skill 适配和未来 Agent 必须消费同一版本化 Core contract；任何 Agent 不得直接写 SQLite、Wiki projection 或原始项目文件。
- **FR-030**：系统必须能生成普通人和 Agent 均可读的 Wiki snapshot/bundle；bundle 是 projection/export，不是写入权威。
- **FR-031**：可移植 bundle 应提供与 Open Knowledge Format v0.2 可映射的 index、Markdown/frontmatter、source、generated/verified、status 和 stale 信息；MemoLens 特有时间证据与类型化关系使用命名扩展，不能牺牲领域精度换取表面兼容。
- **FR-032**：内部页面引用不得要求向 Agent暴露不必要的绝对 Library/DB 路径；Agent 通过稳定 URI/ID 和受控 evidence 工具访问。
- **FR-033**：每个知识单元必须具有 sensitivity/capability 状态，至少能表达 `local_only`、`agent_allowed`、`remote_analysis_approved`、`exportable` 和 `sensitive_personal` 的等价策略。
- **FR-034**：任何交给外部 Agent 的帧、拼图、字幕、音频或短片段必须形成 payload manifest；全库原件不得因建立 Wiki 被无差别上传。
- **FR-035**：Compiler QA 必须检测断链、循环、孤儿、无证据 claim、越权引用、stale source、相互矛盾 current relation 和跨 generation 混用。
- **FR-036**：重复出现的编译错误可以进入持久 issue/error book，但该记录只能约束未来编译和触发修复 proposal，不能自行覆盖用户事实或执行代码。
- **FR-037**：删除或重建 Wiki projection 前后，canonical asset、analysis、user confirmation、project、timeline、export 与 usage facts 必须保持不变并可用 manifest 验证。
- **FR-038**：首阶段不得以独立图数据库、云端全库、通用 Agent runtime 或模型训练为成立条件；这些只在预注册 benchmark 证明必要后考虑。

### Key Entities

- **Media Wiki Generation**：由固定 ledger watermark、schema、compiler、模型/规则和 policy 构建的完整知识视图。
- **Wiki Page / Concept View**：面向人和 Agent 的分层知识单元；携带来源、状态、freshness、revision 和 digest。
- **Directory Index / Atlas Page**：支持渐进披露的目录或主题概览，不是语义真值。
- **Asset**：精确媒体字节身份。
- **Temporal Evidence Span**：Asset 内稳定时间范围及帧、字幕、音频或确定性信号。
- **Observation / Claim**：确定性过程、模型、用户或外部来源对 evidence 的陈述。
- **Typed Relation**：两个知识单元间带证据、authority 和时态的关系。
- **Event / Episode Hypothesis**：跨素材聚合的可修正事件或更长周期。
- **Usage Interval**：某次成功导出使用的原素材时间范围或图片引用。
- **Residual Interval**：在已知使用区间之外仍可供创作的时间范围。
- **Search / Traversal Trace**：Agent 的 query plan、工具步骤、候选路径、预算和停止理由。
- **Refinement Request**：对受限候选窗口获取更多多模态证据的版本化任务。
- **Project Memory Binding**：项目对 Wiki、Creator、Blueprint、Timeline 与 query revision 的冻结引用。
- **Compiler Issue / Error Book Entry**：可复现的 Wiki 结构或语义编译缺陷、根因和修复状态。
- **Portable Wiki Bundle**：可读、可 diff、可交换但不具有写入权威的 projection snapshot。

## Success Criteria

### Measurable Outcomes

- **SC-001**：在至少 200 个经许可/合成的创作检索任务上，复杂任务（≥2 类约束且至少一次 relation traversal）的“5 分钟内找到并加入正确 Timeline span”成功率，相对 lexical、dense RAG、typed hybrid 中最强 baseline 提升至少 10 个百分点；95% paired CI 下界大于 0。
- **SC-002**：简单单条件查询至少 95% 不触发 relation traversal 或远程 refinement；其 p95 延迟不超过最强 baseline 1.25 倍。
- **SC-003**：视频候选 `R@5@IoU≥0.5` 不低于 Spec 010 当前最强时间检索 baseline 3 个百分点以上；Wiki summary 不得以牺牲精确时间定位换取多跳表现。
- **SC-004**：所有返回给 Creative Blueprint/Timeline 的候选具有稳定 asset/span evidence、analysis revision、selection reason 和 coverage；随机人工审计至少 300 个候选，evidence correctness ≥95%。
- **SC-005**：强约束违反率为 0；no evidence、not indexed、source unavailable、permission denied、budget exhausted 和 constraint conflict 分类准确率 ≥98%。
- **SC-006**：在需要 event + usage + script 三类关系的任务上，Agent 最多 8 个知识工具调用完成的比例 ≥90%；达到预算后无限展开或全库 fallback 的次数为 0。
- **SC-007**：正常单资产新增、移动、重分析或使用记录更新不触发全库扫描/N×N 结构；100k fixture 上 affected projection p95 激活时间由技术计划在固定硬件下冻结，且增长不超过 10k fixture 的 4 倍。
- **SC-008**：每个故障注入点 kill/restart 后 current generation 始终是完整 generation；半建页面、dangling current head 和跨 generation relation 被用户读取的次数为 0。
- **SC-009**：用户确认在 rebuild、model upgrade 和 app restart 后保留率 100%；model hypothesis 覆盖 user-confirmed current fact 的次数为 0。
- **SC-010**：创建项目后更新 Wiki/Creator/analysis，再重放旧 project binding，稳定 evidence set、Timeline 输入和 export usage manifest 一致率为 100%。
- **SC-011**：两个兼容 Agent 对相同 pinned query 返回相同 hard-constraint verdict、底层 candidate/evidence IDs 和 coverage；Agent 文案不要求一致。
- **SC-012**：Portable bundle 的页面/frontmatter/index 解析成功率 100%，内部链接有效率 ≥99.9%；所有无效链接必须带已知 dangling 状态而非静默损坏。
- **SC-013**：建立和查询 Wiki 时，未记录 payload 的非 loopback 媒体外发为 0；未授权 whole-file/whole-library upload 为 0。
- **SC-014**：与当前 mixed search 相比，测试用户完成“文稿 8 个段落 → 每段至少一个可用私人画面 → 初剪”的中位主动操作数减少至少 40%，同时平均替换次数不增加。

## Baselines and Experiment Protocol

至少比较四条固定路径：

1. 当前 lexical mixed search。
2. Flat text/caption dense RAG。
3. Spec 009 typed hybrid retrieval，不启用 Wiki traversal。
4. Wiki pages + typed relations + Agent compositional traversal。

所有路径必须共享同一 asset、analysis output、query、Agent/model、时间预算和外发预算。报告简单/复杂、多跳、无答案、已用片段、时间定位、跨模态冲突和部分索引分桶；同时报告质量、工具调用、token、帧/短片读取量、延迟与用户主动编辑数。

Wiki 的收益必须来自知识组织和可组合检索，不能通过给它更强模型、更多帧或更长时间制造不公平提升。

## Rollback and Degradation

- Wiki 作为新 projection shadow 构建，不替换 Canonical Ledger。
- 编译失败时继续使用上一个完整 generation；没有 generation 时回退 Spec 009 typed search。
- 关系层无收益时关闭 traversal，页面仍可作为可读索引。
- 模型分析不可用时保留确定性资产/时间/usage 页面，并明确 semantic coverage gap。
- Portable bundle 生成失败不影响内部 Wiki 与项目；失败工件不得标记为完整快照。
- 独立图存储 bake-off 无收益时继续使用类型化关系 projection，不保留运维复杂度。

## Out of Scope

- 不在本规范中选择数据库、向量引擎、图存储、embedding 或多模态模型。
- 不建立通用互联网知识百科；热点研究与电影专业知识有独立来源和生命周期。
- 不推断真实人物身份或敏感关系作为默认能力。
- 不让 Wiki 自动发布、移动、删除或上传原素材。
- 不要求在首次创作前完成全库最高精度分析。
- 不把 Markdown 文件数量、图节点数量或模型调用量当作创新结果。
- 不允许 Agent 直接执行 shell、任意 FFmpeg 参数或绕过 Core 写数据库。

## Assumptions

- 用户选择一个本地 Library 根目录，媒体可在其下嵌套组织。
- Spec 008 最终提供稳定 asset/source/analysis identity 与可恢复 operation。
- Spec 009 提供 typed intent、honest empty 与统一 evidence result contract。
- Spec 010 提供可验证的 temporal span/event projection；未验证关系保持 hypothesis。
- 外部 Agent 可能支持不同的图片、视频和音频能力，因此 refinement 必须先做 capability discovery。
- 可移植 Wiki 格式不承担百万级细粒度媒体事实的事务主存储。

## Dependencies

- [ML-004](../004-evidence-backed-retrieval-privacy-benchmark/spec.md)：固定数据、baseline、隐私和结果声明。
- [ML-007](../007-local-capability-boundary/spec.md)：约束 Library、媒体读取、Agent payload 与写操作权限。
- [ML-008](../008-unified-media-memory-kernel/spec.md)：提供 canonical ledger、revision、operation 和 projection generation。
- [ML-009](../009-intent-evidence-retrieval/spec.md)：提供 typed query、candidate、reason、error 和 fusion contract。
- [ML-010](../010-hierarchical-temporal-memory/spec.md)：提供 span/event/episode 与 query-guided refinement。
- [ML-011](../011-consentful-creator-model/spec.md)：提供 confirmed Creator Memory，不能覆盖媒体事实。
- [ML-012](../012-verifiable-story-compiler/spec.md)：消费 claim/evidence 并形成可验证创作。
- [ML-015](../015-agent-agnostic-creative-protocol/spec.md)：未来将本 Wiki 绑定到 Agent 无关 CLI 与开放项目链。

## Research References

- [Google Code Wiki](https://developers.googleblog.com/introducing-code-wiki-accelerating-your-code-understanding/)
- [Open Knowledge Format v0.2](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
- [Google Cloud Knowledge Catalog](https://cloud.google.com/blog/products/data-analytics/introducing-the-google-cloud-knowledge-catalog)
- [LLM-Wiki: Retrieval as Reasoning](https://arxiv.org/abs/2605.25480)（2026 预印本，不能当作媒体场景已验证结论）
- [A-MEM, NeurIPS 2025](https://papers.neurips.cc/paper_files/paper/2025/file/19909c36f51abc4856b4560aff3d36d6-Paper-Conference.pdf)
- [HippoRAG 2, ICML 2025](https://proceedings.mlr.press/v267/gutierrez25a.html)
- [ReadAgent](https://deepmind.google/research/publications/74917/)
- OriNodes-v4 current architecture（外部本地参考，未随仓库发布）

## Authorization Boundary

除已单独授权的 [ML-014-A0](slices/014-a0-live-read-surface/spec.md) 只读切片外，本父规范只定义目标、约束、实验和验收，不授权修改数据库、项目格式、模型配置、用户文件或其他产品代码。
