# Feature Specification: Craft Wiki & Technique Compiler

- Feature ID：`ML-016`
- 创建日期：2026-08-22
- 状态：`PROPOSED / EXPERIMENT`
- 实施授权：`NONE`
- 输入决策：[43 问产品对齐记录](../product-decisions-43-questions-2026-08-22.md)
- 相关调研：[Agent 可导航媒体 Wiki 调研](../../agent-media-wiki-research-2026-08-22.md)
- 规范类型：Spec Kit 风格产品规范；具体模型、存储与渲染映射留给 `plan.md` / ADR
- 优先级：P1，建立“普通人可调用的专业剪辑知识”
- 依赖：[ML-011](../011-consentful-creator-model/spec.md)、[ML-012](../012-verifiable-story-compiler/spec.md)、[ML-014](../014-agent-navigable-media-wiki/spec.md)、[ML-015](../015-agent-agnostic-creative-protocol/spec.md)

## Overview

MemoLens 不应把“专业”简化为几个固定模板或炫酷转场。它需要一套可解释、可验证、能根据本次文稿、素材、情绪和渲染能力动态调整的 Craft Wiki；Agent 从中提出 Technique Card，用户选择或委托后，Technique Compiler 再把抽象的电影语言编译成当前项目可执行、可预览、可撤销的 typed edit plan。

产品关系是：

```text
用户的思想、立场与参考
  + 本次 Creative Blueprint
  + 私人素材证据与 Script Coverage
  + 有来源的 Craft Wiki
  + 当前机器可验证的编辑能力
       ↓
可解释 Technique Cards
       ↓ 用户选择、修改或一键委托
项目级 Technique Set
       ↓
typed edit operations → Preview → 反馈 → 新 revision
```

知识必须分为三层，不能混成一个“会自我学习的提示词库”：

1. **Core Craft Wiki**：电影教育、行业实践、可授权资料与产品验证共同形成的基础知识；有来源、适用条件、反例和版本。
2. **Personal Craft Shelf**：用户主动保存、确认喜欢或明确讨厌的技巧、参考和解释；属于 Creator Memory 的受控输入。
3. **Project Technique Set**：为本次视频选定、改写和参数化的技巧，只随当前 Creative Blueprint/Timeline revision 生效。

Technique Card 与 Agent Skill 也必须分开：Card 是“什么效果、为什么、何时适用、当前能做到什么”的声明式知识；Skill 是经过 validator 和测试后执行该技巧的过程能力。没有可验证执行器的 Card 仍可用于解释和分镜建议，但不得标成“可自动实现”。

## Hypothesis and Falsification

### Hypothesis

与固定模板、自由提示词或只给效果名称相比，基于来源、素材条件和当前 Blueprint 生成的 Technique Cards，再经 capability-aware compiler 形成 typed edits，能够让非专业创作者更快做出具有明确叙事目的的初剪，同时理解关键专业选择，而不是得到不可解释的效果堆叠。

### Falsification / Kill Criteria

满足任一条件时，Technique Compiler 不得晋级为默认自动路径：

1. 对相同素材和文稿，专业盲评偏好相对最强 baseline 提升不足 10 个百分点，且普通用户完成时间没有改善。
2. 用户需要撤销或替换的自动技巧比例高于固定基础剪辑 baseline 10 个百分点以上。
3. 系统无法在建议前可靠判断素材、音频或渲染 capability 是否满足，导致承诺可实现但生成失败的比例超过 2%。
4. 技巧解释频繁退化成无来源的审美断言，或参考作品被当作可复制模板。
5. 个性化技巧在没有用户确认时写入长期 Creator Memory，或一个项目的选择污染另一个项目。
6. 引入 Craft Wiki 后，首版初剪的中位时间、错误操作或 UI 复杂度显著高于不用该能力的路径，且渐进披露不能解决。

## User Scenarios & Testing

### User Story 1：用户用效果语言也能得到专业、可理解的建议（Priority: P1）

用户可以说“前面更抓人”“这段更有电影感”“这里要从热闹转到孤独”，而不必先知道 jump cut、J-cut、match cut 或 montage。Agent 结合文稿作用、素材类型、节奏和当前能力，给出少量 Technique Cards；每张说明原理、适用证据、预期效果、风险和自动化等级。

**Independent Test**：让不了解术语的目标用户完成一组情绪与叙事任务，核对推荐是否对应真实素材、解释是否帮助选择、是否承诺了不存在的能力。

**Acceptance Scenarios**：

1. **Given** 用户只描述想要的观感，**When** Agent 推荐技巧，**Then** 每张 Card 用普通语言先说明结果，专业术语与原理可展开。
2. **Given** 同一技巧需要特定景别、运动方向或音频条件，**When** 当前素材不满足，**Then** Card 标为不适用、需替代或仅能手工实现，不伪造适配。
3. **Given** 多个技巧达到相似效果，**When** 返回候选，**Then** 系统解释它们对节奏、连续性、素材需求和实现成本的差异，而不是一次堆满。
4. **Given** 用户要求“直接做一版”，**When** 风险与 capability 可接受，**Then** Agent 可选择默认 Technique Set 并生成 preview；解释仍可追溯，但不强制逐卡确认。

### User Story 2：参考样片被拆成原则，不被整条复制（Priority: P1）

用户提供链接、本地样片、截图、文字说明或让 Agent 搜索公开参考。系统把参考拆为节奏、镜头组织、声音、字幕、构图、色彩和转场等可讨论观察，并说明哪些是事实观察、哪些是解释、哪些在当前素材上能迁移。

**Independent Test**：使用一组授权参考及相似/不相似私人素材，评估观察准确性、迁移合理性、版权边界和生成结果的独立性。

**Acceptance Scenarios**：

1. **Given** 用户提供一条参考视频，**When** 系统分析，**Then** 输出可引用的观察与 Technique Cards，不把整条时间线或原媒体自动复制进项目。
2. **Given** 只能读取网页文字、缩略图或用户口述，**When** Agent 总结，**Then** 明确 analysis coverage，不声称看过未读取的视频细节。
3. **Given** 参考技巧依赖用户没有的镜头，**When** 编译本次方案，**Then** 系统给出保持叙事目的的替代技巧，而不是机械模仿。
4. **Given** 来源许可证不允许复制资产，**When** 用户保存参考，**Then** 只保存链接、引用、用户笔记和变换后的知识，不缓存未经授权的原作品。

### User Story 3：技巧会根据本次文稿和素材动态编译（Priority: P1）

同一张“每句口播后插入 B-roll”的 Card，在不同项目中可以根据句子长度、气口、素材动作峰值、情绪和屏幕方向形成不同的时长、入口和退出点。系统不输出固定秒数模板，而是解释当前参数来源。

**Independent Test**：对同一 Card 配置不同文稿、素材和平台条件，检查输出是否随证据变化、是否保持 hard constraints、是否可用 typed operations 重放。

**Acceptance Scenarios**：

1. **Given** 文稿已有带时间戳语音和气口，**When** 编译 B-roll 插入，**Then** 边界优先对齐语言与动作结构，并保留可解释依据。
2. **Given** 候选素材运动方向与前后镜头冲突，**When** 编译连续性技巧，**Then** 系统调整候选、裁切或放弃该技巧，不默默产生跳轴。
3. **Given** 用户手工微调一个技巧参数，**When** Agent 后续修改别处，**Then** 当前 revision 的调整不会被整条重建覆盖。
4. **Given** Card 需要系统尚不支持的视觉效果，**When** 用户选择，**Then** 系统可以生成说明或分镜建议，但不得提交无法验证的 Timeline operation。

### User Story 4：基础专业能力先于特效炫技（Priority: P1）

首批 Craft Wiki 优先帮助用户完成高频、普遍认可且可检验的基础工作：删停顿与气口处理、画面/声音与文字匹配、B-roll 覆盖、连续性、动作切点、景别变化、基础节奏、字幕可读性、对白/音乐层级、基础色彩一致性和画幅重构。

**Independent Test**：使用没有高级转场、遮罩或生成式特效的基础能力集，完成口播、Vlog、图文故事三类黄金旅程并由普通用户与专业剪辑者双盲评价。

**Acceptance Scenarios**：

1. **Given** 口播存在可安全去除的停顿和填充词，**When** 用户启用精简，**Then** 系统保留语义、自然气口和可撤销边界，不把所有静音一刀切。
2. **Given** 文稿每段已有素材候选，**When** 生成初剪，**Then** 系统先满足语义、声音和连续性，再考虑装饰性转场。
3. **Given** 多机位或 B-roll 有明显动作，**When** 选择切点，**Then** 系统可解释切点与动作/语言/音乐依据。
4. **Given** 高级效果不可用，**When** 生成 first cut，**Then** 基础剪辑仍完整可用，不因缺少特效而阻塞。

### User Story 5：用户在共创中学习，但学习是可选且渐进的（Priority: P1）

Agent 先给结果与理由摘要；用户可以展开术语、原则、反例和参考。用户对 Card 的选择、修改和结果反馈形成项目历史；只有用户明确选择“以后也这样”或确认偏好，才进入 Personal Craft Shelf/Creator Memory。

**Independent Test**：分别用“一键生成”和“深度共创”路径完成同一项目，检查两者是否共用同一知识、项目历史和长期写入规则。

**Acceptance Scenarios**：

1. **Given** 用户只想快速出片，**When** Agent 应用基础技巧，**Then** 不要求先学习术语或逐卡作答。
2. **Given** 用户想理解原因，**When** 展开 Card，**Then** 可看到原理、在当前素材上的证据、风险和替代项。
3. **Given** 用户只在本项目选择某技巧，**When** 项目结束，**Then** 该选择不自动变成长久偏好。
4. **Given** 用户确认“以后我的口播都少用快切”，**When** 写入 Creator Memory，**Then** 保存来源、作用范围、revision 和撤销入口。

### User Story 6：知识与执行能力可以演进而不改写历史（Priority: P2）

新的研究、执行器或 App 版本可以提升 Card；历史项目仍固定当时的 Card revision、参数、capability profile 和 compiled operations。旧 Card 若失效、来源撤回或执行器有缺陷，系统标记状态并提供升级 proposal。

**Independent Test**：更新 Card、撤回来源、改变 capability profile 后重放历史项目，并测试显式升级与回滚。

**Acceptance Scenarios**：

1. **Given** Card 新 revision 改变推荐条件，**When** 打开旧项目，**Then** 旧 Timeline 仍引用旧 revision，不静默重新编译。
2. **Given** 技巧执行器被发现有缺陷，**When** 项目引用它，**Then** 系统提示影响范围和可验证修复 proposal，历史不被删除。
3. **Given** Core Wiki 更新，**When** 用户浏览 Personal Shelf，**Then** 用户笔记和确认不会被覆盖；冲突可见。

## Edge Cases

- 同一个术语在不同剪辑传统、语言或平台中含义不同。
- 参考样片的技巧判断来自字幕或缩略图，而非完整视听证据。
- 技巧本身合理，但与创作者立场、素材真实性或平台无障碍要求冲突。
- 音频节奏与画面动作给出互相冲突的切点。
- 同一镜头同时满足语义却破坏屏幕方向、人物视线或色彩连续性。
- 口播停顿是情绪表达而非应删除噪声；ASR 把喘息、笑声或方言误判为填充词。
- 不同参考来源对同一原则给出冲突建议，或外部网页已失效。
- Card 的解释可用，但当前渲染器只支持其中一部分参数。
- 用户要求完全复刻某个作品、商业模板、字体、音乐或 LUT，但缺少许可。
- 生成式效果会改变人物、地点或事实表达，超出非破坏性剪辑范围。
- 用户在 Timeline 手工改动后，原 Technique Set 的先决条件已不成立。
- 一个项目混用不同 Card revision 或执行器版本。

## Requirements

### Functional Requirements

#### 知识分层与来源

- **FR-001**：Core Craft Wiki、Personal Craft Shelf 与 Project Technique Set 必须具有不同写入权限、生命周期和 revision；不得互相隐式升级。
- **FR-002**：电影专业知识、热点/平台趋势、私人素材事实和 Creator preference 必须属于可区分来源域；检索可以联合，权威和 freshness 不得混淆。
- **FR-003**：每条 Core Craft claim 必须提供来源、观察或产品实验依据、适用范围、局限、revision、review 状态和 freshness；“电影级”“专业”不是独立证据。
- **FR-004**：存在流派、平台或学术争议时必须表达多个观点和条件，不得把审美偏好伪装成普遍事实。
- **FR-005**：外部参考的访问范围、许可证/使用条件和分析时间必须记录；来源失效不删除历史 observation，但会影响 current trust/status。

#### Technique Card contract

- **FR-006**：每张 Card 必须包含稳定 ID、revision、名称、用户可理解的预期效果、专业原理、适用的叙事/情绪功能、素材/声音先决条件、禁忌与失败模式、示例来源、参数范围和替代方案。
- **FR-007**：Card 必须声明 capability level：至少区分 `explain`、`recommend`、`preview`、`compile`、`render`；每一级只能在对应 validator 通过后声明。
- **FR-008**：Card 必须区分硬性先决条件、偏好条件与未知条件；不满足硬条件时不可自动编译。
- **FR-009**：Card 必须能绑定一个或多个 script beat、素材 evidence span、声音 beat、Blueprint intent 与预期 Timeline effect，形成可追溯链。
- **FR-010**：Card 不得直接包含任意 shell、自由 FFmpeg graph、未校验代码或允许 Agent 提权的指令。
- **FR-011**：Technique Card 与 executable Skill 必须分离；Skill 需要版本、输入/输出 schema、effect class、validator、fixture 和回滚语义，Card 不因存在自然语言步骤就自动成为 Skill。

#### 参考解构与版权边界

- **FR-012**：参考分析必须按实际可见证据输出 observation，并区分镜头事实、模型解释、风格推断和可迁移建议。
- **FR-013**：系统不得默认下载、复制、重发或长期缓存无明确许可的完整参考作品、音乐、字体、模板或 LUT；可以保存链接、必要引用、用户笔记和非替代性的结构化观察。
- **FR-014**：系统不得以“参考”为由复刻作品的独特镜头序列、受保护表达或人物风格；必须把可迁移单位收敛为一般原则，并结合用户素材重新设计。
- **FR-015**：Agent 使用网络搜索时，来源、访问时间、证据范围和引用必须进入项目 research snapshot；搜索结果本身不自动成为 Core Craft Wiki。

#### 推荐、编译与执行

- **FR-016**：推荐必须同时读取 Creative Blueprint、Script Coverage、私人素材 evidence、Creator confirmed preference、current capability 与 Card conditions；不得只根据风格关键词选卡。
- **FR-017**：默认推荐数量必须小且可比较，并解释 selection reason、rejected alternatives、风险、素材适配和预期效果；具体数量可由界面与任务调整。
- **FR-018**：一键路径可以自动形成 Project Technique Set，但必须保留所用 Card revision、参数、依据和可撤销 operation。
- **FR-019**：Compiler 只能输出受 Core schema 支持的 typed edit operations；所有时间、素材、字体、颜色、音量、裁切和效果参数在提交前验证。
- **FR-020**：动态参数必须由本次 evidence 导出，例如 speech boundary、motion peak、music beat、shot duration、screen direction 或 subtitle density；固定值只能是明确 fallback，并标记原因。
- **FR-021**：当多个技巧或约束冲突时，系统必须按事实/安全、用户明确要求、可读可听、叙事连续性、Creator confirmed preference 和装饰效果的可解释优先级求解；无法安全解决时返回 conflict。
- **FR-022**：编译结果必须包含 preconditions、affected spans、typed diff、capability profile、validation result、preview binding 和 rollback target。
- **FR-023**：用户对参数或 Timeline 的手工修改必须成为 current revision 的一等输入；后续 Agent edit 不能通过全量重编译静默覆盖。
- **FR-024**：效果在当前设备不可渲染时，系统必须降级为解释、Storyboard proposal、基础替代或明确 unsupported，不能提交伪成功 operation。

#### 基础能力与学习规则

- **FR-025**：P0 Card 集必须优先覆盖：语义/画面匹配、停顿与气口、B-roll、动作切点、J/L 声桥、基础连续性、景别/反应/建立镜头、节奏、字幕可读性与强调、对白/音乐层级、基础色彩一致性、画幅重构。
- **FR-026**：高级遮罩、复杂转场、生成式补帧/改景、人物重绘和第三方插件效果不得成为 P0 基础旅程依赖。
- **FR-027**：用户对 Technique Card 的项目选择、拒绝、微调和结果评分自动记录为 project fact；只有显式“以后也这样”或等价确认才可写入 Personal Shelf/Creator Memory。
- **FR-028**：长期偏好必须可查看来源、适用范围、revision、例外和撤销；不得把观看时长、一次导出或 Agent 推断自动当作确认。
- **FR-029**：UI 与 Agent 必须支持渐进披露：先展示预期效果、适用性和自动化状态，专业术语、理论、反例和来源按需展开。
- **FR-030**：技术说明应帮助用户作创意决定，不得强制通过教程或考试才能一键生成。

#### 版本、验证与治理

- **FR-031**：项目必须固定 Card/Skill revision、compiler revision、capability profile 与输出 operations；知识库更新不改写历史项目。
- **FR-032**：Card、Skill 或来源更新必须产生新 revision/supersession；发现缺陷时标记 affected projects 并生成 proposal，不自动修改 Timeline。
- **FR-033**：Card QA 必须检查缺失来源、条件自相矛盾、无执行能力却声称 render、越权操作、无障碍风险、版权风险和无法重放的参数。
- **FR-034**：Compiler 必须有固定 fixture 与 baseline，分别覆盖口播、Vlog、图文故事、音乐蒙太奇、无答案和能力不足。
- **FR-035**：任何“电影级”“专业级”或质量提升声明必须来自预注册用户/专业盲评，不得由 Card 数量、来源数量或自动化比例替代。

### Key Entities

- **Core Craft Wiki**：有来源、版本与适用条件的通用剪辑/视听表达知识。
- **Personal Craft Shelf**：用户确认保存、偏好或反感的技巧与参考集合。
- **Technique Card**：声明效果、原理、条件、风险、来源和可实现等级的知识单元。
- **Technique Skill**：经验证、可执行且只产生 typed operations 的程序性能力。
- **Reference Observation**：从用户提供或合法访问参考中得到的带覆盖范围观察。
- **Project Technique Set**：绑定当前 Blueprint/Timeline 的 Card revisions 与参数。
- **Capability Profile**：当前 Core/Agent/设备能解释、分析、预览、编译和渲染的能力快照。
- **Compiled Technique Plan**：Card 与项目 evidence 编译后的有前置条件 typed edit 集合。
- **Craft Conflict**：多个技巧、素材事实、用户意图或能力之间无法静默解决的冲突。
- **Learning Explanation**：从当前素材出发、可按需展开的专业选择说明。

## Success Criteria

### Measurable Outcomes

- **SC-001**：100% 可推荐 Card 具有来源、适用条件、反例/风险、revision 和 capability level；缺任一字段的 Card 不进入默认候选。
- **SC-002**：在至少 150 个带素材条件标注的任务上，硬先决条件不满足却被推荐为可自动实现的比例低于 2%，被实际提交为 render operation 的比例为 0。
- **SC-003**：对口播、Vlog 和图文故事各至少 30 个项目，专业剪辑者盲评“叙事/情绪目的被有效实现”的胜率相对固定模板与自由 LLM 指令中最强 baseline 提升至少 10 个百分点。
- **SC-004**：目标非专业用户能正确解释“为什么选这张 Card、它会改变什么、能否撤销”的比例 ≥85%，同时一键路径的首个 preview 中位时间不比无解释路径增加 10% 以上。
- **SC-005**：所有 compiled plan 通过 schema、capability、source span、duration、audio、subtitle 和 effect validator；未校验自由指令进入 renderer 的次数为 0。
- **SC-006**：同一 Blueprint、evidence、Card/Skill revision 与 capability profile 重放时，canonical Timeline digest 一致率 100%。
- **SC-007**：Card 更新、来源撤回或执行器禁用后，历史项目静默改变次数为 0；affected-project 报告准确率 100%。
- **SC-008**：一次项目选择自动写入 Creator Memory 的次数为 0；显式确认偏好的来源与撤销入口完整率 100%。
- **SC-009**：参考分析中，对未读取内容作具体视听断言的比例为 0；未经许可完整参考资产被复制进项目/知识库的次数为 0。
- **SC-010**：相对基础 typed editor baseline，目标用户完成“表达效果 → 选定方法 → 得到可播放版本”的中位主动操作数减少至少 35%，且总撤销/返工次数不增加。

## Baselines and Experiment Protocol

至少比较：

1. 不提供技巧建议，只使用基础手工 Timeline。
2. 固定风格模板/预设参数。
3. 自由 LLM 根据提示直接生成编辑指令。
4. Technique Cards + conditions + capability-aware compiler。

各路径共享相同素材、文稿、参考、渲染能力、Agent/model 和时间预算。盲评必须分开报告普通用户偏好、专业评委对叙事目的/连续性/声音/可读性的判断，以及完成时间、主动操作、撤销、render failure、解释正确性和版权边界。

MovieCuts 等数据证明剪辑类型识别本身仍然困难，不能用“模型识别了一个 cut 名称”替代 MemoLens 的真实项目评测。

## Rollback and Degradation

- 先以只读 Craft Wiki + explain/recommend Card 运行，不开放 compile/render。
- Compiler shadow 生成 typed plan，与当前 Timeline 基线对比；只有 fixture 和盲评过门槛才允许默认应用。
- 某 Card 或 Skill 禁用时，回退基础剪辑或已有已验证 revision，不阻塞项目打开和手工编辑。
- 外部参考不可访问时使用已保存的来源元数据与用户笔记，并明确 coverage；不伪造补全。
- Personal Shelf 损坏或不可用时不影响 Core Wiki、项目历史和素材事实。

## Out of Scope

- 不建立自动抓取、下载或复刻全网作品的模板市场。
- 不把审美争议压成单一“专业分数”。
- 不在本规范选择具体模型、NLE 引擎、插件市场、字体、音乐库或 LUT 供应商。
- 不让 Agent 直接运行任意效果代码或修改原始媒体。
- 不用生成式视频替代用户已有素材，除非未来另立 spec 并明确事实与许可边界。
- 不要求首版支持电影长片、复杂多机位、协同审片或完整专业调色台。

## Assumptions

- ML-014 能返回精确素材 evidence 与 analysis coverage。
- ML-015 提供 Creative Blueprint、统一 operation history 和 capability discovery。
- ML-012/ML-018 能给出 script beat、claim/evidence 与完整 coverage plan。
- 用户可以选择快速委托或逐步共创，两者使用同一 Card/operation contract。
- 外部 Agent 可做参考研究，但 MemoLens 只接收带来源、范围和 schema 的结果。

## Research References

- [MovieCuts: A New Dataset and Benchmark for Cut Type Recognition, ECCV 2022](https://www.ecva.net/papers/eccv_2022/papers_ECCV/papers/136670659.pdf)
- [AVE: Audio Visual Editing Dataset, ECCV 2022](https://arxiv.org/abs/2207.09812)
- [HIVE: Harnessing Multimodal Large Language Models for Video Editing, EMNLP Industry 2025](https://aclanthology.org/2025.emnlp-industry.185/)
- [CoT-Edit: Let CoT Guide Instruction Video Editing, CVPR 2026](https://openaccess.thecvf.com/content/CVPR2026/html/Liang_CoT-Edit_Let_CoT_Guide_Instruction_Video_Editing_CVPR_2026_paper.html)
- [Agent Skills specification](https://agentskills.io/specification)
- [Google Skills repository](https://github.com/google/skills)

## Authorization Boundary

本规范只定义专业知识、推荐、编译、验证和评测边界；不授权修改产品代码、安装依赖、下载外部媒体、执行效果、改变项目格式或写入用户偏好。
