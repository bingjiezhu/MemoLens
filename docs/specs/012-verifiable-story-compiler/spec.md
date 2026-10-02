# Feature Specification: Verifiable Story Compiler

- Feature ID：`ML-012`
- 创建日期：2026-08-20
- 状态：`PROPOSED / EXPERIMENT`
- 实施授权：`NONE`
- 组合决策：[SHOULD-EXPERIMENT](../implementation-decisions-2026-08-20.md)；先扩展现有 brief/timeline 的最小 claim/evidence，完整 Story Graph 与 C2PA 不进入当前主流
- 规范类型：Spec Kit 风格的产品—架构混合规范
- 证据截止：2026-08-20
- 优先级：P2 创作跃迁
- 依赖：[Spec 004](../004-evidence-backed-retrieval-privacy-benchmark/spec.md)、[Spec 007](../007-local-capability-boundary/spec.md)、[Spec 008](../008-unified-media-memory-kernel/spec.md)、[Spec 009-A](../009-intent-evidence-retrieval/spec.md)、[Spec 011-A](../011-consentful-creator-model/spec.md)；Spec 010 是可选 temporal/event evidence adapter
- 标准参考：[W3C PROV-O](https://www.w3.org/TR/prov-o/)、[C2PA 2.4](https://spec.c2pa.org/specifications/specifications/2.4/specs/C2PA_Specification.html)

## Overview

MemoLens 应先由 Story Planner 提出一份可审查的 Story Graph，再由确定性的 Story Compiler 把冻结 Story Graph 编译成 photo set、文案约束、timeline 与 export manifest。Story Graph 由带证据的 claim、beat、shot、transition、audio cue、constraint 和用户决定组成。每条可验证陈述指向具体 asset、视频时间范围、transcript、metadata 或用户提供内容；每个镜头记录选择、裁剪、模型/规则、Creator context 和 revision。

内部 provenance 使用轻量、版本化的 Entity / Activity / Agent / derivation 语义。C2PA 是导出和分享边界的互操作适配层，不是内部真源，也不证明故事陈述在语义上真实。

## Hypothesis and Falsification

### Hypothesis

如果创作流程先生成带 evidence 的结构化 Story Graph，再通过确定性的 Compiler 形成 photo set、timeline 与 export manifest，那么 MemoLens 能降低无证据叙事和编辑成本，支持重放与篡改检测，并形成区别于一次性生成器的可信创作体验。Planner 可以是非确定的，但其输出和完整生成条件必须冻结；Compiler 的确定性从冻结 Story Graph 开始计算。

### Falsification / Kill Criteria

- Benchmark aggregate 的 factual evidence coverage 低于 98%，则自动发布能力停止；无论 aggregate 是否达标，任一单独产物只要存在一个 unresolved factual claim，就只能保持 draft/review-required。
- 引用正确率低于 95% 或 evidence coverage 低于 98%，则 story graph 不得成为发布级 provenance。
- 同一冻结输入无法重放出等价 manifest，且差异无法由明确非确定性字段解释，则 deterministic compiler 假设失败。
- 用户编辑任务没有比 direct-prompt baseline 降低错误或操作成本，或任务完成率、最终 factual error、hard-constraint violation 任一 guardrail 未通过，则高级编译 UI 保持实验，不成为默认工作流。

## User Scenarios & Testing

### User Story 1：用户先看到带证据的故事卡片（Priority: P1）

用户描述“做一支关于第一次独自旅行的 45 秒短片”后，MemoLens 先提出 beat/shot cards。每张卡片显示它表达的 claim、使用的素材与时间范围、证据强度、Creator preference 和缺失内容。

**Why this priority**：先检查故事骨架和证据，比生成完整时间线后再发现错误更高效。

**Independent Test**：对有 ground truth 的素材库生成 story graph，逐条检查 claim、evidence span、constraint 和 missing-evidence 状态。

**Acceptance Scenarios**：

1. **Given** query 和已验证素材，**When** 生成故事计划，**Then** 每个事实性 claim 至少有一个稳定 evidence reference 或标记 unsupported。
2. **Given** “第一次”无法由 library coverage 证明，**When** 生成，**Then** 系统使用限定表述、询问用户确认或标记为 user assertion，不直接当作事实。
3. **Given** 某 beat 没有满足 hard constraint 的素材，**When** 展示计划，**Then** 显示缺失素材，不用无关候选填充。
4. **Given** 使用 Creator preference，**When** 查看卡片，**Then** 显示实际 compiled context revision 和影响字段。

### User Story 2：用户修改故事但不丢失 provenance（Priority: P1）

用户可以替换素材、调整顺序、修改表述、缩短片段或覆盖 soft preference。每次修订形成新 revision，旧 revision 和用户决定仍可查看、比较和恢复。

**Why this priority**：可编辑性和可追溯性是 MemoLens 区别于一次性生成的重要价值。

**Independent Test**：对 claim、shot、order、duration 和 caption 分别修改，检查 revision、diff、evidence 和 replay。

**Acceptance Scenarios**：

1. **Given** 用户替换一个 shot，**When** 保存 revision，**Then** 新 shot 的 evidence 与 derivation 更新，其他未变节点 identity 保持稳定。
2. **Given** 用户覆盖 soft preference，**When** 编译，**Then** override 成为 provenance event，不修改 Creator Profile 历史。
3. **Given** 用户修改事实性 caption，**When** 新文本超出 evidence 支持，**Then** 系统标记 review-required 或要求 user assertion。
4. **Given** 用户恢复旧 revision，**When** 重放，**Then** 使用当时冻结的 evidence、context 和 compiler version，不读取当前 profile 猜测。

### User Story 3：系统阻止无证据内容自动发布（Priority: P1）

用户可以创作诗意、主观或想象性表达，但系统必须区分事实 claim、解释性 claim、用户提供 claim 和纯风格文本。没有支持的事实不能悄悄进入“可验证”状态。

**Why this priority**：Creativity 不要求把所有文字变成事实，但 provenance 必须诚实表达哪些内容来自证据。

**Independent Test**：混合事实、情绪、推断、用户输入和虚构文案，检查 classification、evidence requirement 和发布状态。

**Acceptance Scenarios**：

1. **Given** “照片拍于 2024 年 5 月”且有可信 metadata，**When** 校验，**Then** claim 指向该 metadata assertion。
2. **Given** “这是她最快乐的一天”，**When** 没有用户确认，**Then** 标为 interpretive，不作为可验证事实。
3. **Given** 用户明确提供“这是我第一次独自旅行”，**When** 保存，**Then** 标为 user assertion 并记录确认时间，不伪装成媒体推断。
4. **Given** factual claim 无 evidence，**When** 请求 publish-ready，**Then** 状态保持 blocked/review-required。

### User Story 4：用户可重放同一故事与成片（Priority: P1）

用户在以后打开项目时，可以用冻结的 source、analysis、Creator context、story revision、timeline、render profile 和工具版本重放等价计划与导出；缺失依赖有明确说明。

**Why this priority**：个人媒体项目可能跨多年；current model 或 profile 变化不应改写旧成品。

**Independent Test**：在 profile/model 更新后重放旧项目，比较 story/timeline/export manifest 的稳定字段和 artifact hash。

**Acceptance Scenarios**：

1. **Given** 所有依赖仍可用，**When** 以冻结 Story Graph 重放 Compiler，**Then** Timeline/Compilation Manifest 的确定性字段 hash 一致。
2. **Given** source unavailable，**When** 重放，**Then** 明确指出缺失 asset/span，不自动替换相似素材。
3. **Given** 当前 Creator Profile 已更新，**When** 重放旧 revision，**Then** 使用旧 compiled context manifest。
4. **Given** 用户选择升级 compiler/model 后重新生成，**When** 保存，**Then** 创建新的派生 revision 并保留差异，不覆盖旧版。
5. **Given** 只冻结 Story Graph 与 compiler inputs，**When** 重放，**Then** 只承诺 Timeline/Compilation Manifest 的稳定字段一致，不声称重新调用非确定 Planner 会产生同一 Story Graph。
6. **Given** 要求重放 Planner，**When** 没有冻结完整模型 response、seed、sampling config、prompt/tool result 和 runtime identity，**Then** 系统报告 planner replay unavailable，而不是用缓存最终结果伪装重新生成。

### User Story 5：用户分享可验证的内容来源（Priority: P2）

用户选择带 Content Credentials 导出时，成片或照片集附带关于 source ingredients、编辑 actions、AI 使用和 human oversight 的机器可读 provenance。验证者能检测 manifest 或内容篡改，但界面不把它宣传为“事实真实性证明”。

**Why this priority**：标准化 provenance 提高跨工具可读性，但必须避免错误承诺。

**Independent Test**：对原始导出、篡改 bytes、移除 manifest、重新编码和软绑定恢复分别验证。

**Acceptance Scenarios**：

1. **Given** 用户启用 provenance export，**When** 导出完成，**Then** manifest 描述 ingredients、actions、AI/human oversight 和 MemoLens project derivation。
2. **Given** 导出内容被修改，**When** 验证，**Then** hard binding validation 失败或显示内容已改变。
3. **Given** manifest 被移除但另有允许的 provenance store，**When** 支持软绑定，**Then** 系统可尝试匹配，但不把软绑定当作 hard binding。
4. **Given** manifest 验证完成，**When** 展示结果，**Then** 分别显示 asset binding、signature、credential/trust、timestamp 与 assertion validation 状态，不用一个“未被篡改”结论替代各状态，也不声称故事语义一定真实。

### User Story 6：维护者分别评估 retrieval、story 与 rendering（Priority: P2）

维护者可以判断失败来自素材检索、claim grounding、story organization、timeline compilation 或 artifact export，而不是只看成片主观评分。

**Why this priority**：端到端创作包含多个可独立失败的阶段，必须可定位才能改进。

**Independent Test**：固定 candidate set 测 story compiler；固定 story graph 测 timeline；固定 timeline 测 render/export。

**Acceptance Scenarios**：

1. **Given** retrieval 候选正确但 story claim 错误，**When** 评分，**Then** retrieval 得分不被 story 错误覆盖，story grounding 单独失败。
2. **Given** story graph 正确但 timeline 违反 duration，**When** 校验，**Then** compiler/constraint stage 单独失败。
3. **Given** timeline 和 render 正确但 provenance manifest 不完整，**When** 发布门禁运行，**Then** provenance track 失败，普通本地 draft 仍可保留。

## Edge Cases

- 用户主动创作虚构、诗意、讽刺或非线性故事。
- 一个 claim 需要多个图片/片段联合支持，或 evidence 彼此冲突。
- Metadata 来自相机、sidecar、模型和用户修正，可信级别不同。
- Source asset 被 archive、移动、删除或权限撤销。
- 同一素材经过 crop、speed、color、audio mix、caption 和转码多次派生。
- Timeline 使用同一 asset 的重叠区间或多个 rendition。
- 模型输出非确定，但用户要求重放旧结果。
- Compiler/version/model 不再安装或权重许可证变化。
- 用户修改 caption 后 evidence 不再支持。
- Human confirmation 与后来的用户修正冲突。
- Export 过程中崩溃、磁盘满、manifest 成功但媒体失败或反向情况。
- C2PA 不支持某容器/codec，或外部平台剥离 manifest。
- Soft binding 误匹配相似 rendition。
- Provenance manifest 泄露私人路径、原始提示、人物身份或隐藏项目数据。
- 用户只想本地保存，不希望生成外部可读 provenance。
- 音乐、字体、第三方素材的许可证与媒体 provenance 不等价。

## Requirements

### Functional Requirements

- **FR-001**：系统在形成 photo set、timeline、copy 或 export 前，必须保存可版本化、可审查的结构化故事状态，不得把最终自由文本作为唯一故事真源。非确定的故事提议与确定的产物生成必须可分别观察和评测；本规范分别称其为 Story Planner 与 Story Compiler，具体组件边界留给技术计划。
- **FR-002**：Story Graph 必须能表达 claim、beat、shot、transition、audio/copy cue、constraint、missing evidence 和 user decision。
- **FR-003**：每个事实性 claim 必须引用一个或多个稳定 evidence span，或保持 unsupported/review-required 状态。
- **FR-004**：系统必须区分 factual、interpretive、user-provided 和 stylistic claim，并为每类定义不同证据与展示规则。
- **FR-005**：Interpretive 或 stylistic 文本可以没有事实 evidence，但不得展示为验证事实。
- **FR-006**：User-provided claim 必须记录确认内容、时间、scope 和 revision，不能伪装成媒体自动推断。
- **FR-007**：涉及 first/last/always/never 等全局绝对表述时，系统必须验证 coverage 条件或使用限定语言。
- **FR-008**：每个 shot 必须引用 asset/segment、source range、analysis revision、selection reason 和允许的 transform intent。
- **FR-009**：每个 beat 必须记录目标、支持 claim、shot membership、ordering/duration constraints 和 evidence status。
- **FR-010**：系统必须把 hard constraint、soft preference、Creator context 和用户 override 分开记录。
- **FR-011**：Story Planner 必须使用冻结的 query/result evidence、compiled Creator context 和 projection generation，并把 proposal 保存为新 Story Graph revision；Planner 输出未冻结前不得进入 Compiler。
- **FR-012**：每次 story 修订必须形成新 revision、parent relation、diff 和 immutable provenance；未变 node 保持稳定 identity。
- **FR-013**：替换、重排、裁剪、改写、删除和用户 override 必须成为 provenance activity。
- **FR-014**：事实性文本编辑后必须重新校验 evidence；超出支持范围时进入 review-required。
- **FR-015**：系统必须显示 missing evidence，不得用低证据素材或生成内容静默填充 hard gap。
- **FR-016**：用户显式覆盖 soft preference 可以继续，且 decision 进入 project provenance；hard permission/safety 不可覆盖。
- **FR-017**：Story Graph 到 Timeline 的 compilation 必须可验证 duration、overlap、source availability、format 和 project constraints。
- **FR-018**：相同冻结 Story Graph、Compiler inputs 与 compiler version 必须产生等价 Timeline/Compilation Manifest；允许的非确定字段必须显式列出并排除在 canonical hash 外。
- **FR-019**：旧 project replay 必须使用当时的 evidence、context、Story Graph、compiler/model/profile，不读取 current state 重建历史。只有冻结完整 model response、seed、sampling config、prompt、tool result 与 runtime identity 时才可声称 Planner replay；否则旧 Story Graph 本身就是 replay input。
- **FR-020**：依赖缺失时必须报告具体 asset/span/model/profile，而不是自动替换。
- **FR-021**：升级 compiler/model 重新生成必须创建新的派生 revision并保存对比。
- **FR-022**：内部 provenance 必须表达实体、活动、责任主体、使用、生成、派生和 revision 关系，但不绑定外部序列化格式。
- **FR-023**：内部 provenance 不得保存不必要的绝对私人路径、secret、token、完整远端 prompt 或敏感未确认身份。
- **FR-024**：每个导出必须提供非循环、可独立验证的内容身份关系，分别覆盖签名前媒体字节、外部 provenance assertions 和最终签名产物。任何已签名层不得把尚未形成的最终身份当作同层字段；具体 canonicalization、hash 拓扑和连接方式留给技术计划。
- **FR-025**：C2PA 或其他外部 provenance 必须作为 opt-in/export policy 支持的 adapter，不能成为内部 Story Graph 权威 schema。
- **FR-026**：外部 manifest 必须准确声明 AI 使用和 human oversight；缺信息时不能臆造。
- **FR-027**：系统必须清楚区分 manifest validation、asset binding 和 semantic truth；验证成功不等于 claim 真实。
- **FR-028**：Hard binding 与 soft binding 的用途必须区分，soft binding 不得替代 hard binding 做篡改保证。
- **FR-029**：外部平台剥离 manifest 时，系统必须说明 provenance 已丢失或仅能通过可选匹配恢复。
- **FR-030**：Provenance export 失败不得留下“已验证”状态；媒体、manifest/assertion store 与最终 rename/publish 必须有一致终态。必须覆盖 media success/manifest failure、manifest success/final rename failure、disk full 和 crash 的 fault-injection，任何失败不得暴露半发布组合。
- **FR-031**：系统必须分别评估 retrieval grounding、claim coverage/correctness、story constraint、timeline validation、render integrity 和 provenance completeness。
- **FR-032**：每个单独 publish-ready artifact 的 unresolved factual claim 必须为 0；Benchmark aggregate 的 98% evidence coverage 只衡量系统能力，不能放宽逐产物发布门禁。
- **FR-033**：本地 draft 和不带外部 manifest 的普通导出仍可用，但内部 project provenance 不应丢失。
- **FR-034**：任何标准/SDK 升级必须通过 compatibility、privacy、tamper 和 rollback 测试。
- **FR-035**：Claim type 必须接受独立人审，分别报告 factual/interpretive/user-provided/stylistic 的 precision、recall、混淆矩阵与严重度；系统不得通过把无证据事实重标为 interpretive 来提高 evidence coverage。
- **FR-036**：C2PA/外部 provenance 验证必须分别报告 binding、signature、credential/trust、timestamp 与 assertion 状态；合法重编码并生成新派生 manifest、无声明字节修改、manifest 删除、签名失效和 soft-binding 误配必须是不同 threat case。

### Key Entities

- **Story Graph**：一份可版本化、可校验、可编译的故事中间表示。
- **Story Planner**：可以非确定地提出 Story Graph 的阶段；可重放性取决于是否冻结完整模型响应与生成条件。
- **Story Compiler**：把冻结 Story Graph 和依赖编译成确定性 Timeline/Compilation Manifest 的阶段。
- **Claim**：带类型、文本/语义、evidence、status 和 confidence state 的陈述。
- **Beat**：故事目的、claim 与 shot 的组织单元。
- **Shot**：对 asset/segment 的选取、范围、转换意图和 provenance。
- **Evidence Link**：Claim/shot 到 asset、segment、transcript、metadata 或 user assertion 的关系。
- **Constraint**：时长、顺序、平台、排除、权限或创作 preference。
- **User Decision**：确认、覆盖、替换、改写或拒绝的不可变活动。
- **Compilation Manifest**：Story Graph 到 photo set/timeline/copy 的输入、版本、规则与结果 hash。
- **Artifact Manifest**：Render/export 的 ingredients、actions、工具版本、unsigned payload hash、assertion-store hash、final signed artifact hash 和 derivation。
- **External Provenance Envelope**：面向 C2PA 或其他标准的导出适配结果。
- **Validation Record**：claim、timeline、artifact 和 provenance 的分阶段结果。

## Success Criteria

### Measurable Outcomes

- **SC-001**：Benchmark aggregate 中可验证 factual claim 的 evidence coverage 至少 98%；未覆盖部分全部处于 unsupported/review-required。每个单独 publish-ready artifact 的 unresolved factual claim 为 0。
- **SC-002**：按 claim type、evidence source、语言和严重度分层抽样至少 200 条 claim，由两位独立标注者判断 type 与 evidence entailment，分歧经第三方 adjudication；factual classification precision/recall 与 evidence correctness 均至少 95%，高严重度事实 recall 为 100%。
- **SC-003**：所有最终 shot 对 asset/segment、source range、analysis 和 transform 的可追踪率为 100%。
- **SC-004**：所有 artifact 对 story/timeline/compiler/context/render revision 的可追踪率为 100%。
- **SC-005**：相同冻结 Story Graph、Compiler inputs 与版本的 Timeline/Compilation Manifest canonical hash 一致率为 100%；Planner 只有在完整模型 response/seed/sampling/prompt/tool/runtime 冻结时才进入 replay 指标。
- **SC-006**：冻结 threat matrix 中的 unsigned media byte flip、manifest mutation/removal、signature invalid、credential untrusted/revoked、timestamp invalid 与 soft-binding mis-match 均被正确分类，检测率为 100%；合法重编码加新派生 manifest 被识别为新 derivation，不误报成原 artifact 未改变。
- **SC-007**：External provenance validation 时，UI 对 binding、signature、credential/trust、timestamp、assertion 与 semantic truth 的状态展示正确率为 100%，把任一 validation 结果误表述为语义真实性证明的次数为 0。
- **SC-008**：Embedded/external provenance 开销按媒体类型验收：photo 不超过 `max(256 KiB, unsigned payload 的 5%)`，video 不超过 `max(512 KiB, unsigned payload 的 2%)`，独立 sidecar 不超过 512 KiB 加每个 evidence reference 64 bytes；原始 evidence 媒体本身不计入 metadata。
- **SC-009**：缺失 source/model/profile 的 replay 100% 返回具体诊断，不自动替换素材。
- **SC-010**：固定 candidate 的 story benchmark、固定 story 的 timeline benchmark、固定 timeline 的 render/provenance benchmark 均可独立运行。
- **SC-011**：任一单独 artifact 存在至少 1 个 unresolved factual claim 时，自动 publish-ready 次数为 0；aggregate 98% coverage 不构成例外。
- **SC-012**：三次固定实验均通过 coverage、correctness、replay、tamper 和隐私门槛后，才允许启用发布级 provenance。
- **SC-013**：在不少于 30 个 paired 编辑任务的 intention-to-treat 集合中，相对 direct-prompt baseline，修正 factual error 与满足 hard constraint 所需的中位用户操作数至少降低 20%，paired bootstrap 95% CI 下界大于 0；未完成或用户放弃的任务按预注册最大操作成本计入，并计为任务失败。方案相对 baseline 的完成率差值 95% CI 下界不得低于 -2 个百分点，最终 factual error rate 差值 95% CI 上界不得高于 +2 个百分点，最终 hard-constraint violation rate 不得增加；任一条件未达到则高级编译 UI 保持实验。
- **SC-014**：media success/manifest failure、manifest success/rename failure、disk full 和 process crash 各 100 次 fault-injection 中，半发布或错误“verified”状态次数为 0；重试后最多存在一个 committed artifact set。

## Baseline and Experiment Protocol

至少比较 direct prompt → final copy/timeline、现有 rules director、Story Graph without evidence gate、Evidence-first Planner + Compiler。固定 retrieval candidate set 隔离 Planner，固定 Story Graph 隔离 Compiler，固定 Timeline 隔离 render/provenance。

用户任务评估至少记录：发现错误所需时间、替换/修订操作数、是否完成或放弃、未完成原因、unsupported claim、最终 factual error、hard-constraint violation、最终接受率和对 evidence explanation 的理解。主分析按全部随机分配任务执行；不得只分析成功修正的幸存任务。主观叙事质量可另测，但不能替代 grounding 门槛。

## Rollback and Degradation

- Story Graph 作为新 revision，不覆盖现有 brief/timeline。
- Evidence gate 未通过时保留 draft/review-required，不阻止用户本地手动编辑。
- External provenance adapter 不可用时可以普通导出，但明确不附带 Content Credentials。
- 新 compiler/model 未通过门槛时回到上一个验证 version。
- C2PA/SDK 升级失败时保留内部 provenance，不让外部格式破坏项目可读性。
- Source unavailable 时保持 project 和 tombstone，不自动换素材。

## Out of Scope

- 不证明媒体或故事的语义真实性。
- 不对第三方音乐、字体或素材许可证做自动法律判断。
- 不要求所有艺术性文本都有事实 evidence。
- 不在本规范中选择生成模型、timeline 格式、C2PA SDK 或签名基础设施。
- 不把外部 provenance 设为所有本地 draft 的强制项。
- 不允许 provenance 代替用户审核。

## Assumptions

- Spec 008 至 011 提供稳定 evidence、intent、event 和 Creator context identity。
- 用户理解 factual、interpretive 和 user assertion 的不同展示。
- 某些平台会剥离 metadata，因此本地 project provenance 仍是长期真源。
- 签名身份、证书和发布密钥管理需要单独的 release/security plan。
- C2PA 当前版本和 SDK 会继续演进，adapter 必须可替换。

## Dependencies

- Spec 004 提供 claim、citation、replay、tamper、cost 和 privacy benchmark。
- Spec 007 控制 source read、provider、export 和签名相关 capability。
- Spec 008 提供 asset/analysis/operation/artifact ledger。
- Spec 009 提供 candidate evidence、constraint 和 fusion trace。
- 现有 segment evidence 足以启动 012-A；只有使用 event/episode story 时才依赖 Spec 010 对应 evidence span。
- Spec 011-A 提供 compiled Creator context 与 confirmed preference provenance；012-A 不依赖主动学习。
