# Feature Specification: Consentful Creator Model

- Feature ID：`ML-011`
- 创建日期：2026-08-20
- 状态：`PROPOSED / EXPERIMENT`
- 实施授权：`NONE`
- 组合决策：[MUST-STAGED / 011-A Context Compiler](../implementation-decisions-2026-08-20.md)；在 008-A/009-A 后实施字段分流，完整遗忘分期，主动学习与 opaque personalization 保持实验
- 规范类型：Spec Kit 风格的产品—架构混合规范
- 证据截止：2026-08-20
- 优先级：P2 个性化跃迁，先 shadow 后 opt-in
- 依赖：[Spec 004](../004-evidence-backed-retrieval-privacy-benchmark/spec.md)、[Spec 007](../007-local-capability-boundary/spec.md)、[Spec 008](../008-unified-media-memory-kernel/spec.md)、[Spec 009-A](../009-intent-evidence-retrieval/spec.md)；Spec 010 仅是 event/person feedback 的可选 evidence adapter
- 研究依据：[隐私与主动学习](../../frontier-research-2026-08-20.md#9-隐私与主动学习)

## Overview

MemoLens 应把 Creator Memory 从一组确认字段升级为可同意、可解释、可限定范围、可撤销的创作者模型。系统可以从用户明确选择、二选一偏好、相关/不相关反馈和项目修订中提出 hypothesis，但未确认推断不能静默成为稳定画像。

Creator context 分为三层：用户确认的长期 profile、项目级 override、当前 task intent。编译后再分成 retrieval constraints、directing/ranking policy、copy/tone policy 和 render/output constraints，防止 `9:16`、platform 或文案语气错误影响素材事实检索。

## Hypothesis and Falsification

### Hypothesis

如果系统只请求少量高信息、多样且低负担的反馈，并把学习限定为透明融合权重、规则、关系或轻量适配层，那么每个用户/任务 strata 在最多 30 次可计数反馈操作内，可以改善预注册的个性化检索主指标，同时不损害创作 guardrail、非个性化结果或用户控制。

### Falsification / Kill Criteria

- 同等反馈预算下，主动选样相对随机选样的 primary effect 低于 5%，或 paired 95% CI 下界不大于零，则停止该选样策略。
- 每 1% primary metric 相对提升需要超过 10 次可计数反馈操作，回答率低于 60%，或退出率高于 20%，则不进入默认 Inbox。
- 个性化 holdout 回归超过 2 个百分点，或出现未确认 preference 被应用，则立即回退。
- 无法解释某个 active preference 的来源、范围、版本和影响，则该 preference 不得进入 compiled context。

## User Scenarios & Testing

### User Story 1：用户看见并控制系统学到的内容（Priority: P1）

用户可以查看稳定偏好、项目偏好和系统提出的 hypothesis，知道每项来自哪些确认或行为，并能确认、拒绝、修改、暂停或撤销。

**Why this priority**：个人创作偏好高度敏感；可见与可撤销是学习功能的前提。

**Independent Test**：创建 explicit preference、行为 hypothesis、冲突更新和撤销，检查状态、provenance、当前生效范围和旧 artifact。

**Acceptance Scenarios**：

1. **Given** 系统从多次选择推断“更喜欢低饱和”，**When** 用户打开 Creator Memory，**Then** 它显示为未确认 hypothesis，并列出依据和置信范围。
2. **Given** 用户确认 preference，**When** 新项目编译 context，**Then** 只在声明的任务/场景范围内应用。
3. **Given** 用户拒绝或撤销 preference，**When** 后续 context 编译，**Then** 不再应用；历史 artifact 仍保留原 revision provenance。
4. **Given** 两项 preference 冲突，**When** 查看 profile，**Then** 系统显示冲突、有效时间和 resolution，不静默覆盖旧值。

### User Story 2：用户的格式偏好不会污染事实检索（Priority: P1）

用户设置 platform、aspect ratio、语气或节奏时，系统把它们用于合适的 directing、copy 或 render 阶段；只有明确的内容偏好和排除项影响素材检索。

**Why this priority**：当前 Photo path 把多个 profile 字段拼入 retrieval prompt，造成职责混合。

**Independent Test**：逐项修改 Creator field，比较 compiled retrieval、directing、copy 和 render outputs。

**Acceptance Scenarios**：

1. **Given** 只把 aspect ratio 从 1:1 改为 9:16，**When** 编译 context，**Then** retrieval facts/constraints 不变，render/output policy 变化。
2. **Given** 新增 `must_exclude: selfie`，**When** 编译，**Then** exclusion 进入 retrieval clause，并有 source preference provenance。
3. **Given** 更改文案语气，**When** 检索同一 query，**Then** candidate set 不因语气字段改变。
4. **Given** 同一 stable profile 的项目级 override，**When** 项目结束，**Then** override 不自动升级为全局 preference。

### User Story 3：用户用少量反馈改善结果（Priority: P1）

Media Inbox 只提出少量高价值问题，例如人物合并、事件边界、相关/不相关或两组故事/照片二选一。用户能跳过问题，系统不会因为沉默而推断同意。

**Why this priority**：大量手工标签不可持续；主动学习只有在反馈负担低且真正优于随机时才有价值。

**Independent Test**：对固定用户模拟器和真实许可试验分别运行 active、random、no-learning 三组相同预算实验。

**Acceptance Scenarios**：

1. **Given** 多个低置信候选，**When** 选择下一问题，**Then** 同时考虑预期信息增益、代表性、多样性和用户负担，不连续询问近重复问题。
2. **Given** 用户跳过，**When** 更新模型，**Then** 不把跳过记为正/负偏好。
3. **Given** 用户回答二选一，**When** 更新，**Then** 只更新声明范围内的 preference/fusion hypothesis，并可回滚。
4. **Given** 问题涉及敏感人物或地点，**When** 用户关闭该类学习，**Then** 不再生成相关问题或使用相关派生数据。

### User Story 4：用户理解某项偏好如何改变结果（Priority: P1）

用户可以查看“应用此偏好”和“不应用此偏好”的可比较结果，知道哪些素材、排序、文案或输出发生变化，再决定是否确认。

**Why this priority**：个性化影响必须可验证，不能只显示抽象权重。

**Independent Test**：对单一 preference 生成 counterfactual diff，冻结 query、library generation 和其他 context。

**Acceptance Scenarios**：

1. **Given** 一个 proposed preference，**When** 用户预览影响，**Then** 系统展示受影响阶段、结果差异和证据，不改变正式 profile。
2. **Given** preference 对结果无可测影响，**When** 预览，**Then** 系统明确显示无变化，不诱导用户确认。
3. **Given** preference 导致 hard constraint 违反，**When** 编译，**Then** preference 被忽略并显示冲突，不能覆盖安全/权限/事实约束。
4. **Given** 多项 preference 同时作用，**When** 查看解释，**Then** 可以定位主要贡献和交互，不只显示一个总分。

### User Story 5：用户能导出、暂停和遗忘个人模型（Priority: P1）

用户可以导出可读 profile 与 history、暂停所有学习、删除特定 preference 或请求完整遗忘；删除传播到派生模型和 active context，但不伪造历史 artifact 的来源。

**Why this priority**：个人模型应属于用户，并具备明确生命周期。

**Independent Test**：暂停、删除一项、删除一个项目范围、完整遗忘、恢复备份，并检查 active context、projection、adapter 和 artifact provenance。

**Acceptance Scenarios**：

1. **Given** learning paused，**When** 用户继续创作，**Then** 系统不产生新 hypothesis，但可按用户选择使用已确认 profile。
2. **Given** 用户删除 preference，**When** 删除闭包完成，**Then** active context、个人化 projection 和轻量适配层不再包含它。
3. **Given** 旧成品曾使用已删除 preference，**When** 查看 provenance，**Then** 只显示当时使用过的 opaque revision ID、删除状态和最小时间信息，不保留已删除 preference value/evidence，也不重写历史成品。
4. **Given** 用户导出 profile，**When** 检查，**Then** 内容可读、版本化，不包含不可解释权重或未授权敏感数据。

### User Story 6：维护者证明个性化没有伤害通用能力（Priority: P2）

维护者在上线任何学习策略前，使用 user-specific test、non-personalized holdout、random baseline、rollback 和 drift 检查证明收益。

**Why this priority**：反馈模型容易过拟合，用户短期行为也不一定代表稳定偏好。

**Independent Test**：固定反馈预算和用户模拟器，比较 active/random/no-learning，随后在不同主题 holdout 和时间切片上复测。

**Acceptance Scenarios**：

1. **Given** 个性化训练集提升，**When** holdout 退化超过门槛，**Then** 策略保持 shadow 或回退。
2. **Given** preference 分布随时间改变，**When** drift 超过门槛，**Then** 系统降低置信、请求确认或停用，不自动扩大影响。
3. **Given** 新策略没有优于 random，**When** 实验结束，**Then** kill criteria 生效，不以主观 demo 推进。

## Edge Cases

- 一次项目的临时风格被误认为长期偏好。
- 用户故意选择反常素材用于实验或客户需求。
- 两个平台、客户、语言或内容类型的偏好相反。
- 用户与协作者在共享项目中给出不同反馈。
- 用户跳过、撤销、undo 或在离线期间多次修改。
- Preference 的有效时间改变，例如过去喜欢但现在不喜欢。
- 少量反馈被 burst/近重复素材放大。
- Active learner 连续询问同类问题或只选最不确定的异常样本。
- 模型从人物、生物特征、精确地点、健康、宗教、性取向、政治观点或其他敏感信号推断创作者身份特征。
- Profile revision 更新时旧页面、job 或 project 仍在使用旧 revision。
- Counterfactual 两侧使用了不同 library generation 或模型版本，造成假差异。
- 删除 preference 后仍留在缓存、adapter、export、benchmark、原始日志、结构化事件、query/model/tool trace、crash report、诊断包或 telemetry spool。
- 用户导入旧 profile，与当前 schema 或 policy 冲突。
- Preference confidence 被错误展示为精确概率。

## Requirements

### Functional Requirements

- **FR-001**：Creator context 必须区分 confirmed stable profile、project override、current task intent 和 unconfirmed hypothesis。
- **FR-002**：未确认 hypothesis 不得进入默认 active context；用户沉默、跳过或未查看不等于同意。
- **FR-003**：每项 preference 必须记录 stable ID、value/schema、scope、status、valid time、observed time、confidence state、source evidence 和 revision。
- **FR-004**：Preference 更新必须通过 new revision、supersedes/invalidates 或 explicit resolution 表达，不得原地抹掉历史。
- **FR-005**：System-generated hypothesis 必须列出支持与反例、影响阶段和确认动作。
- **FR-006**：系统不得从媒体或行为推断、学习或固化生物识别/人物身份、种族/族裔、国籍、宗教、健康/残障、性取向/性生活、政治观点、工会、精确持续位置或其他政策定义的敏感属性为 Creator preference。用户主动提供的敏感内容只能进入显式、专用、最小 scope 的 user-provided instruction，默认本地加密、排除学习与 provider payload，且不得被转换为 inferred stable preference。
- **FR-007**：Context compiler 必须将输入分配到 retrieval、directing/ranking、copy/tone 和 render/output policy，不得把全部字段拼成一条自由文本。
- **FR-008**：Aspect ratio、platform、format 和纯 copy tone 默认不得改变事实/素材 retrieval candidate semantics。
- **FR-009**：Must-include、must-exclude 和明确内容 preference 可以影响 retrieval，但必须成为可检查 clause 并受 Spec 009 hard/soft 语义约束。
- **FR-010**：每次编译必须记录 compiler version、profile ID/revision/hash、project override、applied/ignored fields 和 conflict resolution。
- **FR-011**：Artifact 必须引用实际使用的 compiled context manifest，而不是读取当前 profile 推测历史。
- **FR-012**：Project override 在项目外默认不生效；升级为 stable preference 必须显式确认。
- **FR-013**：系统必须支持 preference scope 至少按 task、media type、project/client、platform 和时间限定。
- **FR-014**：Feedback history 必须不可变、带 source result/evidence 和当时 context；撤销后原记录及其撤销关系仍可审计，不能修改或删除旧记录来伪造历史。撤销记录的具体表示留给技术计划。
- **FR-015**：主动问题候选必须来自可解释来源，例如 retriever 分歧、校准不确定、邻域不确定、事件冲突或覆盖多样性。
- **FR-016**：问题选择必须同时考虑信息增益、多样性、代表性、敏感性和用户负担；不能只按最大 entropy。
- **FR-017**：系统必须限制问题频率、总预算和连续同类问题，并支持 skip、pause 和 category opt-out。
- **FR-018**：二选一、相关/不相关、人物合并和事件修正必须更新不同类型的 preference/evidence，不得压成一个总分。
- **FR-019**：学习优先作用于可解释 fusion、selection、rule 或轻量 adaptation；任何 opaque update 仍必须可版本化、回滚和 benchmark。
- **FR-020**：每次学习产生新 model/preference revision，并保留触发 feedback set、训练/更新配置和 holdout 结果。
- **FR-021**：新 revision 在进入 active context 前必须通过 user-specific、non-personalized holdout、random baseline 和 privacy gate。
- **FR-022**：系统必须支持 shadow mode，在不改变用户结果的情况下生成 candidate personalized output 和差异。
- **FR-023**：Counterfactual 比较必须冻结 query、library/projection generation、模型、其他 context 和随机种子。
- **FR-024**：用户必须能看到某 preference 影响了哪些素材、排序、文案或输出；无可测影响时明确显示。
- **FR-025**：Preference 不得覆盖权限、安全或有证据的 hard fact constraint。
- **FR-026**：用户可以确认、拒绝、编辑、暂停、撤销、按 scope 删除和完整遗忘 Creator model。
- **FR-027**：删除必须传播到 active context、personalized projection、adapter、cache、benchmark、future training、受控备份，以及任何可能保存 preference value/evidence 的原始日志、结构化事件、日志索引、query/model/tool trace、crash report、诊断/support bundle、telemetry payload 或本地 spool。Retention inventory 必须逐项把载体标为 `never-write-sensitive-value` 或纳入删除闭包；无法受控删除的外部载体不得接收这些敏感值。旧 artifact 的 tombstone 只可保留 opaque preference/revision ID、deleted status、created/deleted time 与最小 artifact reference，不得保留 preference value、evidence、敏感来源文本或可逆派生物。
- **FR-028**：Profile 导出必须可读、版本化、包含 provenance，并排除未授权敏感派生数据和不可解释秘密权重。
- **FR-029**：Import 旧 profile 必须验证 schema、scope、来源与冲突，不能静默覆盖当前确认。
- **FR-030**：Drift 必须降低 confidence、请求再确认或停用，不得自动扩大 preference scope。
- **FR-031**：所有个性化指标必须按用户、task、时间和反馈预算报告，不能只报告总体平均。
- **FR-032**：任何默认学习上线均必须满足本规范三次固定实验和 Spec 004 声明门槛。
- **FR-033**：Sensitive user-provided instruction 与 learnable preference 必须使用不同 schema、purpose、retention 和 compiler channel；前者不得成为主动学习标签或跨 scope ranking feature。
- **FR-034**：敏感 preference/adapter 删除后，MemoLens 控制范围内的任何残留副本必须不可再被解密或重新激活；恢复任何旧备份时必须先应用当前删除状态。达到该结果的密钥和恢复机制留给技术计划。
- **FR-035**：本地删除 SLA 为：active context 1 秒内停用，在线 cache/projection/adapter 5 分钟内清除，primary store、benchmark、日志索引、原始日志、query/model/tool trace、crash report、诊断/support bundle 和 telemetry payload/spool 24 小时内完成，受控备份最迟 30 天或下一轮轮换中清除；用户导出或外部分发副本不可由本地删除召回，界面必须在删除前说明。
- **FR-036**：反馈预算中的一个 operation 是用户针对已展示问题执行一次 answer、skip、reject、confirm、correction 或 undo；仅 view、scroll、hover 与系统重试不计数。一个 question impression 只在问题完整可见时计入 shown denominator。

### Key Entities

- **Creator Profile**：一组已确认、版本化、分 scope 的稳定 preference。
- **Preference Assertion**：value、scope、状态、双时间、来源和 supersede 关系明确的单项偏好。
- **Preference Hypothesis**：系统提出、未确认且默认不生效的候选偏好。
- **Project Override**：仅在项目或客户范围内生效的临时选择。
- **Task Intent**：本次任务的即时指令，不自动成为长期记忆。
- **Compiled Creator Context**：按 retrieval/directing/copy/render 分流后的不可变 manifest。
- **Feedback Event**：用户对结果、候选、人物、事件或 preference 的不可变动作；撤销必须保留原动作并形成可审计的反向关系。
- **Active Question**：带信息增益、负担、敏感级别和预算的待确认问题。
- **Personalization Revision**：由一组 feedback 与配置生成、可回滚的学习状态。
- **Counterfactual Diff**：在冻结其他变量时，有/无某 preference 的结果差异。
- **Deletion Tombstone**：只保留 opaque ID、删除状态、最小时间与 artifact reference，不保留已删除 preference 值或 evidence。
- **Sensitive User Instruction**：用户主动提供、专用 scope、默认本地且不可学习的敏感内容；与 Creator preference 分开。

## Success Criteria

### Measurable Outcomes

- **SC-001**：未确认 hypothesis 被默认 active context 应用的次数为 0。
- **SC-002**：Aspect ratio/platform/copy tone 单字段变化导致事实 retrieval candidate set 变化的次数为 0；明确 retrieval preference 除外。
- **SC-003**：每个用户/任务 strata 最多 30 次可计数反馈操作后，唯一 primary endpoint `preference-conditioned retrieval nDCG@10` 相对 no-learning baseline 提升至少 10%；创作偏好匹配、hard constraint 与 privacy 作为必须同时通过的 guardrail，不能替换 primary endpoint。
- **SC-004**：在至少 30 个独立 user/task strata、三次固定运行上，相同反馈预算的 active selection 相对 random selection 的 primary metric 提升至少 5%，paired bootstrap 95% CI 下界大于 0；否则触发 kill criteria。
- **SC-005**：以完整可见的 question impression 为分母，answer rate 至少 60%、skip rate 与 exit rate 分开报告且 exit rate 不超过 20%；每 1% primary metric 相对提升平均消耗不超过 10 次 FR-036 定义的 operation，view/scroll 不计数且 skip/undo 计入负担。
- **SC-006**：Non-personalized holdout 回归不超过 2 个百分点，hard constraint violation 为 0。
- **SC-007**：Confirm、reject、undo、scope delete、pause 和 full forget 的 active-context 生效准确率为 100%。
- **SC-008**：所有 artifact 对实际 compiled context ID/revision/hash 的可追踪率为 100%。
- **SC-009**：随机抽查 100 个 active preference，来源、scope、状态、影响阶段和回退均可解释，缺失为 0。
- **SC-010**：Counterfactual diff 在冻结条件下可复现率为 100%，无 preference 时的 baseline 不被污染。
- **SC-011**：对冻结 retention inventory 做删除前后内容扫描；active context、personalized projection、adapter、cache、benchmark、future-training reference、原始/结构化日志、日志索引、query/model/tool trace、crash report、诊断/support bundle、telemetry payload/spool 和到期备份的闭包覆盖率与清除率均为 100%。标为 `never-write-sensitive-value` 的载体命中 preference value/evidence 的次数为 0；tombstone 泄露案例为 0，恢复旧备份后重新激活已删除 preference 的次数为 0。
- **SC-012**：三次固定实验均达到收益、负担、holdout 和隐私门槛后，功能才可从 shadow 升级为 opt-in active。
- **SC-013**：删除/遗忘 100% 满足 active 1 秒、online derivative 5 分钟、primary store 及全部受控日志/trace/diagnostics/telemetry 载体 24 小时、backup 30 天的 SLA；对已导出/外部分发副本的不可召回边界披露率为 100%。
- **SC-014**：敏感 user-provided instruction 被学习为 preference、进入主动学习标签或未经 grant 发给 provider 的次数为 0。

## Baseline and Experiment Protocol

至少比较 no-learning、random questions、uncertainty-only、uncertainty+diversity、full active policy。固定用户模拟器、反馈预算、library generation、query set 和初始 profile；按用户、任务类型与初始偏好稀疏度分层，使用相同 strata 做 paired 比较。每个 run 预注册唯一 primary endpoint、secondary guardrail、最小 effect、95% CI 方法与 seed；真实用户试验必须另有同意与退出机制。

检索与创作分别评分。Profile field compiler 需要 metamorphic tests：改变 output-only field 不改变 retrieval；改变 must-exclude 只影响相应 clause；旧 revision replay 不受 current profile 更新影响。

删除实验必须冻结 retention inventory 及其内容 hash，覆盖正常、debug、crash、provider failure、support export 和 telemetry opt-in 路径。每个载体要么证明从不写入 preference value/evidence，要么执行删除、备份恢复和内容扫描；不能只验证索引消失。

## Rollback and Degradation

- 全部学习先运行 shadow，不改变正式结果。
- 新 revision 未过 gate 时继续使用上一个 confirmed revision。
- Active policy 不优于 random 时退回用户主动编辑 profile，不继续自动提问。
- Drift、冲突或解释缺失时降低为 hypothesis 并请求确认。
- 删除/暂停服务不可用时，停止应用 personalization，不能继续使用无法撤销的缓存状态。
- Base retrieval 与创作功能必须在无 Creator model 时完整可用。

## Out of Scope

- 不训练或维护用户人格、心理、人口或敏感身份画像。
- 不把用户主动提供的敏感项目内容转化为可学习、跨项目复用的 Creator preference。
- 不从沉默、停留时长或背景行为推断稳定偏好，除非以后另有明确 opt-in spec。
- 不在本规范中选择学习算法、adapter 或模型。
- 不允许 preference 覆盖权限、安全和事实 evidence。
- 不保证用户偏好永久稳定。

## Assumptions

- 用户愿意在有明显价值时回答少量低负担问题。
- Creator preference 属于敏感本地数据，默认不发送给远端 provider。
- 当前 Creator Profile revision/provenance 可作为 confirmed state 起点。
- Event/person feedback 依赖 Spec 010 的 evidence identity，不把自动 hypothesis 当作确认。
- 指标提升必须在项目数据上验证，论文反馈增益不能直接外推。

## Dependencies

- Spec 004 提供 user-specific、holdout、random baseline、反馈预算和隐私 benchmark。
- Spec 007 管理敏感 profile、feedback 与远端 provider capability。
- Spec 008 提供不可变 preference/evidence、operation 和 artifact identity。
- Spec 009 提供 retrieval clause、fusion trace、counterfactual result contract。
- 011-A Context Compiler 不依赖 Spec 010；仅当产品启用 event/person feedback 时，Spec 010 才提供对应 evidence 与修正关系。
- Spec 012 消费 compiled context manifest，但不读取当前 profile 重写历史。
