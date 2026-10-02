# Feature Specification: Unified Media Memory Kernel

- Feature ID：`ML-008`
- 创建日期：2026-08-20
- 状态：`PROPOSED`
- 实施授权：`NONE`
- 组合决策：[MUST-STAGED，最高架构优先级](../implementation-decisions-2026-08-20.md)；008-A 先做 canonical photo bridge/durable job，008-B 再做 embedding/projection
- 规范类型：Spec Kit 风格的产品—架构混合规范
- 证据截止：2026-08-20
- 优先级：P0 架构收敛
- 依赖：[Spec 004-A](../004-evidence-backed-retrieval-privacy-benchmark/spec.md)、[Spec 007-A](../007-local-capability-boundary/spec.md) 的最小能力收口；不依赖 007-B 最终 identity contract
- 审计依据：[两代数据模型审计](../../architecture-review-2026-08-20.md#p0-a1两代数据模型和非原子双写)

## Overview

MemoLens 需要一个统一媒体记忆内核，让当前支持的图片、视频共享 library、asset、source、analysis、operation 和 projection 语义，并为独立音频保留同一 contract 的扩展点。一次导入或重分析必须产生一个权威结果；Search、Atlas、Inbox current view、Codex 和创作界面只能读取可重建 projection，不再各自维护真源。用户 Review、Creator Profile、事件修正和创作 provenance 属于 canonical facts，不是可随视图删除的数据。

本规范不要求大爆炸数据库迁移，也不要求分布式服务。目标是在本地进程内建立清晰的权威账本、持久任务和模块边界，并通过兼容 projection 逐步替换 legacy `image_index`、同步照片 job 和 Atlas 私有 schema。

## Hypothesis and Falsification

### Hypothesis

如果所有媒体写入先形成内容寻址 Asset、受控 Source 和不可变 Analysis revision，并在同一提交中记录 projection 变更，那么图片与视频可以共用恢复、检索、Atlas、Creator 和 provenance，而不再发生半写或不同页面看到不同资产。

### Falsification

任一受支持 ingest 路径仍能产生只在某套检索或 Atlas 可见的媒体，或故障注入后存在 orphan/half-write，或删除 projection 后不能完整重建，则统一内核没有成立。

## User Scenarios & Testing

### User Story 1：用户只导入一次，所有界面看到同一资产（Priority: P1）

用户通过桌面照片索引、视频导入、浏览器兼容入口或未来系统媒体库导入字节完全相同的文件时，MemoLens 识别为同一个 asset，并在 Library、Search、Atlas、Inbox、Codex 和 Create 中使用一致身份与状态。视觉上相同但字节不同的转码、裁剪或编辑版本是不同 asset，由显式 derivation/duplicate relation 连接。

**Why this priority**：当前图片可能只进入 legacy index 或只进入 media asset，造成可见性和元数据分叉。

**Independent Test**：分别通过每个 ingest surface 导入相同和不同媒体，比较 canonical identity、analysis head 和所有 projection 的最终可见性。

**Acceptance Scenarios**：

1. **Given** 字节完全相同的内容通过两个 surface 导入，**When** 两个操作完成，**Then** 只有一个 canonical asset，source observation 可分别保留。
2. **Given** image 通过 media import 进入，**When** 查询 Search 和 Atlas，**Then** 它与旧照片入口导入的 image 具有相同可见性语义。
3. **Given** source path 改名但内容不变，**When** 重新发现，**Then** asset identity 保持稳定，source observation 更新且有历史。
4. **Given** 内容在同一路径被替换，**When** 重新发现，**Then** 新内容不会覆盖旧 asset 的历史分析。
5. **Given** 同一画面的转码、裁剪、Live Photo 配对或近重复 burst，**When** 导入完成，**Then** 每个不同字节内容保留独立 asset，并用 typed variant/derivation/duplicate relation 表达联系，不合并 provenance。

### User Story 2：用户的照片与视频分析可恢复（Priority: P1）

用户导入大量媒体时，可以暂停、取消、重启应用或经历 provider timeout；系统从安全 checkpoint 恢复，不在 HTTP 请求中长时间阻塞，也不重复发布同一 analysis revision。

**Why this priority**：照片处理目前是同步串行路径，新视频路径已经证明持久 job 更可靠。

**Independent Test**：在 discovery、identity、probe、analysis、embedding、projection 和 publish 每一阶段杀进程，再重启并核对操作状态和最终 revision。

**Acceptance Scenarios**：

1. **Given** 正在分析的照片 job，**When** backend 在任一 checkpoint 崩溃，**Then** 重启后 job 可恢复或确定失败，且没有重复 head。
2. **Given** 文件在 hash 后被替换，**When** 分析准备发布，**Then** job 进入 `source changed` 结果，不发布不匹配 analysis。
3. **Given** 用户取消 job，**When** worker 到达安全 checkpoint，**Then** 操作在有界时间内停止，partial artifact 不成为 current head。
4. **Given** provider timeout，**When** HTTP client 查询 job，**Then** 请求线程不被模型调用长期占用，进度和错误可观察。

### User Story 3：维护者可以删除并重建 Search/Atlas projection（Priority: P1）

维护者或恢复工具可以按明确 allowlist 删除损坏、过期或旧版本的 Search、Atlas、Inbox current view，再从 canonical ledger 重建，且不删除 ReviewDecisionRevision、CreatorProfileRevision、用户事件修正、analysis 历史或创作 provenance。

**Why this priority**：projection 只有可重建，才能真正与业务真源分离。

**Independent Test**：记录 projection hash，删除全部 projection，离线重建并比较内容、generation 和用户可见结果。

**Acceptance Scenarios**：

1. **Given** canonical ledger 完整但 Atlas projection 缺失，**When** 重建，**Then** 所有符合当前 generation 的资产恢复可见。
2. **Given** projection algorithm/model version 变化，**When** 系统检查状态，**Then** projection 被标记 stale，不与新 generation 静默混用。
3. **Given** 重建进程中断，**When** 用户继续使用当前 projection，**Then** 仍读取上一个完整 generation，不看到半建结果。
4. **Given** 两个 rebuild 同时请求，**When** 执行，**Then** 只有一个有效 generation 构建或它们被安全串行化。
5. **Given** 删除所有 allowlisted projection，**When** 比较删除前后的 canonical manifest，**Then** canonical row count 与确定性 hash 完全不变。

### User Story 4：用户切换 Library 不会串库（Priority: P1）

用户在多个 library 之间切换时，运行中请求、进度、缓存、Creator context 和导出不会写入或显示到错误 library。

**Why this priority**：当前 UI 同时维护多份路径和 DB 状态，补偿逻辑复杂。

**Independent Test**：在 active job、慢请求、poll、render 和 Creator update 期间切换 library，注入旧 scope response。

**Acceptance Scenarios**：

1. **Given** Library A 的慢请求，**When** 用户切到 B 后 A 返回，**Then** B 的 UI 和 ledger 不接受该结果。
2. **Given** A 的 active job，**When** 用户切到 B，**Then** job 继续绑定 A 或明确暂停，不会改写 B。
3. **Given** 同一路径后来指向另一 database identity，**When** 旧 operation 提交，**Then** identity mismatch 使其失败。
4. **Given** 应用重启，**When** 恢复 active library，**Then** UI、backend、Electron 和 plugin 使用同一个 opaque library identity。

### User Story 5：维护者安全切换模型和向量空间（Priority: P1）

维护者升级视觉、文本、重复检测或音频模型时，旧向量保持可解释，不同空间不混算，重分析可以增量执行并回退。

**Why this priority**：当前 Atlas 会补零/截断混合向量，默认 text-derived fallback 又被当作视觉信号。

**Independent Test**：同时存在不同 model、dimension、preprocess 和 fallback 产物，运行 Search、duplicate 和 Atlas，确认只在兼容 space 内比较。

**Acceptance Scenarios**：

1. **Given** 两个不同 embedding space，**When** 计算相似度，**Then** 系统拒绝直接比较或使用预声明融合，不做补零/截断。
2. **Given** text-derived fallback，**When** 运行 visual duplicate，**Then** 该信号不被标为视觉或实例相似证据。
3. **Given** 新模型未通过 benchmark，**When** 用户使用默认 profile，**Then** current head 仍指向已验证 space。
4. **Given** 模型升级失败，**When** 回退，**Then** 旧 analysis 和 projection 仍可读取。

### User Story 6：所有客户端消费同一稳定 contract（Priority: P2）

React、Electron、Codex plugin 和 Photon 通过稳定、版本化的 asset/operation contract 读取状态，不直接理解 SQLite 私有表或拼接本地路径。

**Why this priority**：当前 plugin 和 TS/Python response normalization 会放大 schema drift。

**Independent Test**：对相同 contract fixture 运行 Python、TS、MCP 和 Bot contract tests；删除内部列不影响客户端，改变公开字段会触发版本门槛。

**Acceptance Scenarios**：

1. **Given** repository 内部 schema 迁移，**When** public contract 不变，**Then** 所有 surface tests 继续通过。
2. **Given** public contract breaking change，**When** 生成兼容性报告，**Then** 必须提高 contract version 或提供适配层。
3. **Given** plugin 尝试读取未公开内部表，**When** policy test 运行，**Then** 构建失败。

## Edge Cases

- 字节相同但文件名不同，或视觉语义相同但转码、裁剪、编辑 bytes 不同；Live Photo 配对和 sidecar。
- 文件 inode 改变但内容相同，或 hard link 指向同内容。
- Source 暂时卸载、权限撤销、网络盘断开或 case normalization 变化。
- Analysis 部分成功，例如 transcript 成功而 visual 失败。
- 同一 asset 同时触发 import、reindex、refinement 和 delete。
- Provider 响应成功但本地提交前崩溃。
- Operation response 丢失，客户端使用相同或不同 idempotency identity 重试。
- Projection generation 构建完成但 active pointer 切换前崩溃。
- 旧 schema、损坏 migration、磁盘满、SQLite busy 和只读文件系统。
- 同一秒创建多个 job/run，不允许时间戳 ID 冲突。
- 模型产物 dimension 相同但语义空间不同。
- Creator confirmation 引用即将被 supersede 的 analysis。
- 删除 asset 时仍被 timeline、story 或导出 provenance 引用。
- Plugin 版本比 backend contract 旧或新。

## Requirements

### Functional Requirements

- **FR-001**：Canonical Asset 必须表示精确字节内容身份，以完整内容摘要和媒体类型判定；路径只是 source observation，不是 asset 主键。转码、裁剪、编辑、Live Photo 配对与感知近重复不得共享 Asset identity，必须用 typed Variant/Derivation/Duplicate relation 连接。
- **FR-002**：每个 library 和 database 必须具有 opaque、稳定且可验证的 identity，不以 raw path 作为 scope authority。
- **FR-003**：Source observation 必须记录 root identity、相对位置、内容身份、可用性和观察时间，并能表达移动、缺失、改变和撤销。
- **FR-004**：每次分析必须形成不可变 revision，并记录 input asset identity、profile、模型/规则、各阶段状态和来源。
- **FR-005**：Current analysis 必须通过显式 head/pointer 选择；partial、failed 或来源不匹配的 revision 不得成为 current。
- **FR-006**：Canonical mutation、operation state 和 projection-change record 必须原子提交或全部失败。
- **FR-007**：Legacy index、Search、Atlas、Inbox current view 和其他明确列入 projection deletion allowlist 的 read model 必须可重建，不得独立拥有媒体事实。
- **FR-008**：任一 projection 必须有 schema、algorithm、model/space、source ledger position 和 generation identity。
- **FR-009**：Projection rebuild 必须构建新 generation 并原子激活，不能在用户可读 generation 上原地半更新。
- **FR-010**：系统必须支持从 canonical ledger 删除全部 projection 后完整重建，并生成可比较 manifest。
- **FR-011**：当前支持的照片和视频必须共享一个持久 operation/job 生命周期，至少表达 queued、running、partial、failed、cancelled、interrupted 和 succeeded；独立音频在另行定义 ingest/ASR/privacy 用户故事前只保留 contract extension point，不计入本 P0 完成范围。
- **FR-012**：长分析不得在交互式 HTTP 请求生命周期内同步完成；客户端通过 operation identity 观察进度和结果。
- **FR-013**：Job 必须具有稳定唯一 ID、attempt、checkpoint、heartbeat、cancel request、error outcome 和恢复规则。
- **FR-014**：Job 必须在读取与发布前验证 source identity；不匹配时不得发布 analysis。
- **FR-015**：同一幂等 operation 重试不得产生重复 asset、analysis head、project revision 或 response 语义。
- **FR-016**：模型/向量产物必须记录 space ID、model ID、revision、dimension、dtype、preprocess、input hash 和生成状态。
- **FR-017**：不同 space 不得直接计算相似度；相同 dimension 不等于兼容 space。
- **FR-018**：Text-derived、visual semantic、instance duplicate、audio、OCR 和 people-sensitive signal 必须保持可区分来源。
- **FR-019**：模型或 space 升级必须先产生新 revision/projection，不得覆盖旧产物；默认切换受 Spec 004 门槛约束。
- **FR-020**：Library 切换必须使 UI、backend、Electron、worker、plugin 和缓存使用同一个 scope identity。
- **FR-021**：旧 scope response、event、poll 和 mutation 不得被新 active library 接受。
- **FR-022**：所有 runtime-owned resource 必须有幂等 close/retire 生命周期；连续 reload 不得累积 FD、模型或 client。
- **FR-023**：SQLite 访问必须共享一致的 transaction、foreign key、busy、snapshot 和 migration 行为。
- **FR-024**：Migration 必须有 version、checksum、precondition、interruption recovery 和 rollback/forward-fix 说明。
- **FR-025**：Public HTTP、IPC、MCP 和 Bot contract 必须版本化，并与内部 repository object 解耦。
- **FR-026**：客户端不得依赖内部表名、绝对文件路径或未版本化 JSON shape。
- **FR-027**：错误结果必须包含稳定 code、retryability、operation/library scope 和安全 detail；fallback 必须成为显式 stage outcome。
- **FR-028**：用户确认、Creator preference、brief、timeline 和 story provenance 引用稳定 evidence/revision，不引用可被原地覆盖的 row。
- **FR-029**：删除、撤销和 archive 必须定义对 source、analysis、projection、operation 和 creative reference 的行为。
- **FR-030**：迁移期间必须提供旧路径对照、差异工件、切换门槛和回退路径，直到 legacy write 被证明可移除。
- **FR-031**：ReviewDecisionRevision、CreatorProfileRevision、用户 EventCorrection/UserAssertion 和 Creative Revision 必须是 canonical ledger entity；Inbox current view 只能引用这些事实，不得拥有唯一用户决定。
- **FR-032**：Projection 删除必须使用版本化 allowlist；删除或重建前后必须验证 canonical entity 的行数与确定性内容摘要不变。

### Key Entities

- **Library**：用户批准的一组媒体源、active database identity 和 policy scope。
- **Asset**：由精确媒体 bytes 标识、与路径分离的内容身份。
- **Variant / Derivation / Duplicate Relation**：连接不同字节 Asset 的转码、裁剪、编辑、Live Photo 配对、实例近重复或视觉相似关系，不改变各自 provenance。
- **Source Observation**：某时间在某批准 root 观察到 asset 的事实。
- **Analysis Revision**：针对固定 input 和 profile 生成的一组不可变观察/派生产物。
- **Analysis Head**：当前被产品 surface 使用的验证 revision。
- **Embedding Artifact**：带完整 space/model/preprocess provenance 的向量产物。
- **Operation**：可恢复、可取消、幂等的业务动作和状态机。
- **Checkpoint**：Operation 已安全完成、可用于恢复的位置。
- **Change Record**：与 canonical mutation 同事务提交的 projection 变更意图。
- **Projection Generation**：由特定 ledger position、schema、algorithm 和 model 构建的完整 read model。
- **ReviewDecisionRevision**：用户对资产、候选或 Inbox 项作出的不可变确认、拒绝或修正，是 canonical fact。
- **CreatorProfileRevision**：用户确认的创作者偏好 revision，是 canonical fact；current profile 只是其选择视图。
- **User Event Correction / Assertion**：用户对事件、人物、关系或时间的不可变修正与来源。
- **Public Contract**：surface 消费的版本化 DTO 和错误语义。
- **Evidence Reference**：指向稳定 asset、analysis revision、segment 或 observation 的引用。

## Success Criteria

### Measurable Outcomes

- **SC-001**：所有当前支持的 image/video ingest surface 对 byte-identical fixture 产生一个 canonical asset，重复数为 0；semantic-equivalent 但 bytes 不同的 fixture 必须产生独立 asset 且 typed relation 完整率为 100%。
- **SC-002**：在每个写阶段注入失败后，orphan asset/source/analysis/head/change record 数为 0。
- **SC-003**：在 Spec 004 冻结的 `fixture × archive/review/analysis state × surface` expected matrix 中，media import image 与 legacy photo import image 的 Search、Atlas、Inbox current view 和 Create 行为匹配率为 100%。
- **SC-004**：删除全部 allowlisted projection 后可完整重建；排除 generation ID、wall-clock timestamp 和展示布局坐标后的 canonicalized projection manifest hash 一致，布局只按预注册邻域保持率/位移容差验收。
- **SC-005**：Projection rebuild 中断或并发时，用户读取半建 generation 的次数为 0。
- **SC-006**：在每个 job stage 杀进程并重启，100% operation 最终进入可解释终态，重复 current revision 数为 0。
- **SC-007**：Source 在 hash 后被替换、rename 或 symlink race 时，不匹配 analysis 发布数为 0。
- **SC-008**：不同 embedding space 的直接 cosine/补零/截断混算次数为 0。
- **SC-009**：连续 100 次 runtime reload 并静默 60 秒后，已退休 runtime 的 FD/model/client 数均为 0；进程总 FD 不高于首个稳定 active runtime 基线 +2，active model/client instance 不超过各 registry 声明上限，且 20 次移动窗口无正增长斜率。
- **SC-010**：切换 library 后，旧 scope response/event/mutation 被新 scope 接受的次数为 0。
- **SC-011**：Public contract fixture 在 React、Electron、Python、Codex 和 Photon 中的兼容测试通过率为 100%。
- **SC-012**：迁移 shadow 观察至少持续 14 天且覆盖至少 10,000 次 ingest/query/review/create 操作，并在 1k/10k/100k fixture 上各完成一次对照；差异按 expected、known-compatible、bug、security 四类记录，未解释差异为 0 才能关闭 legacy write。

## Rollback and Degradation

- 迁移采用 parallel read/shadow projection/diff/default switch/retire，不进行一次性表替换。
- 新 projection 失败时继续读取上一个完整 generation。
- 新 model/space 未通过门槛时，current head 保持旧验证 revision。
- New job runner 不稳定时，可暂停新任务并让旧数据只读；不能退回非原子双写作为长期路径。
- Canonical ledger migration 失败时必须恢复迁移前快照或执行验证过的 forward-fix。
- Legacy adapter 只在明确观察期保留；新功能不得继续依赖 legacy-only 字段。

## Out of Scope

- 不在本规范中决定物理表设计、ORM、消息队列、向量引擎或目录名称。
- 不要求拆成网络微服务。
- 不要求一次迁移全部历史数据。
- 不定义检索融合算法、Creator learning 或故事生成行为。
- 不把 projection rebuild 等同于删除用户确认或创作历史。
- 不在本 P0 中新增 standalone audio ingest、ASR 或音频隐私产品能力；只保证未来接入不需要另一套身份和 job 语义。

## Assumptions

- 单机本地事务数据库仍能满足当前资产规模的 canonical ledger。
- Search/Atlas 等高成本索引可以作为独立 projection 或 side index。
- 现有新媒体 revision、idempotency 和 FD identity 行为可作为迁移基线。
- Legacy surface 可以在一段时间内通过 contract adapter 继续工作。
- 文件系统是原始媒体事实源，但 ledger 负责内容身份、观察和派生历史。

## Dependencies

- Spec 007-A 先提供当前 identity 上的 runtime/root/DB 最小能力收口；Spec 007-B 与本规范共同收敛最终 library/database/root/operation contract。
- Spec 004-A 提供当前 golden/privacy baseline；完整 fault injection、migration、determinism、性能和质量门槛按 008-A/008-B promotion 阶段扩展。
- Spec 009 至 013 必须只依赖本规范公开的 canonical identity、operation、generation 与 evidence contract。
