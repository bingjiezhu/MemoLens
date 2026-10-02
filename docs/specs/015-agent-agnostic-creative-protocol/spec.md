# Feature Specification: Agent-Agnostic Creative Protocol & Open Project Chain

- Feature ID：`ML-015`
- 创建日期：2026-08-22
- 状态：`A0+A1+B0+B1 IMPLEMENTED / VALIDATED; A2 IN IMPLEMENTATION / PHASE 0-1; B1A LOCAL GO; B2A+B2B+B2B2+B2B3 IMPLEMENTED / LOCAL VALIDATION WITH RESIDUALS; B2B4+B2C0 IMPLEMENTED / PARENT PROMOTION BLOCKED; B2B4A+B2B4B+B2B4B.1+B2B4C IMPLEMENTED / VALIDATED_CONTROLLED_LOCAL; REMAINDER PROPOSED`
- 实施授权：[ML-015-A0 跨 Agent 项目恢复读表面](slices/015-a0-cross-agent-project-resume/spec.md)、[ML-015-A1 Creative Blueprint shadow/validation](slices/015-a1-creative-blueprint-shadow-contract/spec.md)、[ML-015-A2 Plugin-First Library Bootstrap](slices/015-a2-plugin-first-library-bootstrap/spec.md)、[ML-015-B0 Canonical Blueprint Proposal Ledger](slices/015-b0-canonical-blueprint-proposal-ledger/spec.md)、[ML-015-B1 Paired Agent Proposal & Decision Authority](slices/015-b1-paired-agent-decision-authority/spec.md)、[ML-015-B1A Authority Runtime Hardening](slices/015-b1a-authority-runtime-hardening/spec.md)、[ML-015-B2A Canonical Blueprint Workspace](slices/015-b2a-canonical-blueprint-workspace/spec.md)、[ML-015-B2B Deterministic Timeline Lowering](slices/015-b2b-deterministic-timeline-lowering/spec.md)、[ML-015-B2B2 Timeline Reconciliation & Read-only Inspection](slices/015-b2b2-timeline-reconciliation-inspection/spec.md)、[ML-015-B2B3 Canonical Timeline Revision Edit](slices/015-b2b3-canonical-timeline-revision-edit/spec.md)、[ML-015-B2B4 Codex / DeepSeek Canonical Editor Handoff](slices/015-b2b4-codex-deepseek-canonical-editor-handoff/spec.md)、[ML-015-B2B4A Verified Visual Replacement Review](slices/015-b2b4a-verified-visual-replacement-review/spec.md)、[ML-015-B2B4B Safe Canonical Source Playback Grant](slices/015-b2b4b-safe-canonical-playback-grant/spec.md)、[ML-015-B2B4B.1 Preview Transport Audio Truth Correction](slices/015-b2b4b1-preview-transport-audio-truth/spec.md)、[ML-015-B2B4C Canonical Structural Edit](slices/015-b2b4c-canonical-structural-edit/spec.md) 与 [ML-015-B2C0 Canonical Timeline History & Restore](slices/015-b2c0-canonical-timeline-history-restore/spec.md)
- 输入决策：[43 问产品对齐记录](../product-decisions-43-questions-2026-08-22.md)
- 规范类型：Spec Kit 风格产品规范
- 优先级：P0 产品主链与 Agent 接入边界
- 依赖：[Spec 005](../005-video-creative-workbench.md)、[Spec 008](../008-unified-media-memory-kernel/spec.md)、[Spec 014](../014-agent-navigable-media-wiki/spec.md)

## Overview

MemoLens 的默认交互由用户已经选择的外部 Agent 提供，首批目标是 Codex 和 DeepSeek Harness；MemoLens 不再内置第二套聊天、模型账户或通用 Agent scheduler。任何兼容 Agent 都通过同一版本化 CLI/Core contract 读取素材 Wiki、创建 Creative Blueprint、提出素材匹配、生成或修订 Timeline，并把变化显示在同一个 Browser Canonical Editor 中。Electron 只保留原生授权、本地 runtime/broker 与兼容 reader 职责，不是主剪辑界面。

产品主链必须同时满足两种使用深度，但不能割裂成两个产品：

```text
一句意图 / 文稿 / 参考
  → Agent 可以直接给出完整 first cut
  → 用户随时回到任一决定继续讨论
  → 对话修订与可视化手工编辑形成同一 operation history
  → 预览、比较、撤销、恢复、导出
```

对话不是项目真源。Creative Blueprint、素材 evidence、Timeline 和 operation history 才是可重放状态。CLI 也是薄客户端：它不能直接修改 SQLite、原始媒体或任意项目 JSON，所有写入必须经过 MemoLens Core 的统一命令、校验、权限和 revision 规则。

## Hypothesis and Falsification

### Hypothesis

如果 MemoLens 提供 Agent 无关、结构化、版本化的创作协议，同时让对话与可视化工作台共享一个开放项目链，那么用户可以保留自己熟悉的 Agent，仍获得比内置孤立聊天更连贯、更可回退的专业创作体验。

### Falsification

- Codex 和 DeepSeek Harness 对同一项目必须使用不同数据库或不同项目格式才能完成核心旅程。
- 对话操作与 UI 操作产生不同真源、无法按统一顺序撤销，或旧操作重放得到不同状态。
- 用户必须额外配置 MemoLens 模型 API key 才能完成首条语义初剪。
- 从安装后选择一个文件夹到生成可播放 first cut 仍要求用户理解 database path、Python runtime、provider 或内部 schema。
- 开放项目包不能在干净环境被 validator 读取、迁移和解释。

## User Scenarios & Testing

### User Story 1：用户只告诉 Agent 素材文件夹就能开始（Priority: P1）

用户安装 MemoLens 后对 Codex、DeepSeek Harness 或其他兼容 Agent 说“我的素材在这个文件夹”。Agent 调用 MemoLens 连接 Library；若操作系统需要授权，只出现一次与该目录对应的原生确认。系统说明素材数量、基础可用时间和完整分析预计耗时，然后开始渐进索引。

**Why this priority**：用户明确要求冷启动尽量只做这一步。

**Independent Test**：在干净支持设备上，仅提供安装包、一个素材目录和兼容 Agent，完成连接、状态查看、首批搜索与项目创建。

**Acceptance Scenarios**：

1. **Given** MemoLens 首次运行，**When** 用户把一个目录告诉 Agent，**Then** 系统完成或引导完成唯一一次 Library 授权，不要求数据库路径或模型 key。
2. **Given** 目录有大量媒体，**When** 连接成功，**Then** 用户立即看见数量、阶段、预计时间和“可先开始”的范围。
3. **Given** Agent 传入未获批准的目录，**When** 请求连接，**Then** Core 不静默接受；用户可用一次清楚的原生操作批准准确目标。
4. **Given** 用户切换 Agent，**When** 新 Agent 查询状态，**Then** 它读取同一 Library identity 和 operation 状态，不重建第二个库。

### User Story 2：用户可以直接得到完整 first cut，也能逐步共创（Priority: P1）

用户给出一句发布意图、文稿、口播稿、指定素材或参考样片。Agent 可以一次生成选题/脚本建议、Creative Blueprint、素材匹配和可播放初剪；用户也可以要求先讨论某一步再继续。

**Why this priority**：一键快速和深度共创必须是一条连续链，而不是互斥模式。

**Independent Test**：同一输入分别走“直接 first cut”和“逐步确认”两条交互路径，比较最终 Blueprint/Timeline contract 和可回退性。

**Acceptance Scenarios**：

1. **Given** 用户只给一句意图并要求“先做一版”，**When** evidence 足够，**Then** Agent 可直接产生完整 draft 和 preview，不在选题/脚本处强制阻塞。
2. **Given** 用户给出完整口播稿，**When** 生成，**Then** Blueprint 保留原文、分段、每段素材 evidence、未覆盖 gap 与假设。
3. **Given** 用户要求先讨论立场或选题，**When** 该阶段未确认，**Then** 系统可以停在对应 Blueprint revision，不强迫生成 Timeline。
4. **Given** 用户对自动建议无兴趣，**When** 选择跳过解释，**Then** 仍可快速预览；解释和专业细节保持可展开。

### User Story 3：对话修改与工作台手工修改共用一条历史（Priority: P1）

用户可以说“第二段节奏慢一点”、在 Storyboard 替换素材、在 Timeline 修剪片段或调整字幕。每个动作都形成一个 operation；用户可以逐步撤销、重做、查看版本、恢复旧版本或从旧版本分叉。

**Why this priority**：用户明确要求 Google Docs/Slides 式可追溯链，而不是复杂的编辑锁。

**Independent Test**：交替执行 Agent 和 UI 操作，逐步 undo/redo、恢复/分叉，再重放同一操作日志。

**Acceptance Scenarios**：

1. **Given** Timeline revision 5，**When** Agent 和 UI 依次作三次修改，**Then** 历史按真实顺序显示 actor、意图、typed diff、输入与输出 digest。
2. **Given** 用户撤销 Agent 的一次修改，**When** undo 完成，**Then** 系统创建可审计的新状态而非删除历史。
3. **Given** 两个客户端基于旧 revision 同时修改，**When** 第二个提交，**Then** Core 返回 revision conflict 与可比较 diff，不静默覆盖。
4. **Given** 用户恢复到旧版本并继续编辑，**When** 新修改保存，**Then** 分叉关系可见，原分支仍可重放。

### User Story 4：用户能在 Codex 与 DeepSeek Harness 之间继续同一个项目（Priority: P1）

用户可以在 Codex 开始项目，随后让 DeepSeek Harness 查看当前 Blueprint、素材选择、Timeline 和未解决问题。两个 Agent 不共享聊天历史也没关系，因为项目状态和 evidence 是开放、结构化且带版本的。

**Why this priority**：Agent 供应商不能成为项目记忆和编辑历史的锁定点。

**Independent Test**：Agent A 创建到 revision N，Agent B 在新会话中仅通过 MemoLens contract 恢复上下文、解释当前状态并提交 revision N+1。

**Acceptance Scenarios**：

1. **Given** 新 Agent 没有旧聊天记录，**When** 打开项目，**Then** 它能从 project summary、Blueprint、evidence、Timeline 和 open decisions 恢复必要上下文。
2. **Given** Agent B 不支持视频直接输入，**When** 请求精查，**Then** capability discovery 指出缺口并使用支持的 frame/panel 路径或明确降级。
3. **Given** 两个 Agent 对同一 evidence 有不同建议，**When** 用户选择其一，**Then** 选择只更新项目 revision，不把另一建议删除或升级成长期偏好。

### User Story 5：高影响动作清楚确认，普通创作不被授权弹窗淹没（Priority: P1）

搜索、读取 Wiki、生成 draft 和在项目内创建可撤销 revision 应保持流畅；导出到用户目录、覆盖文件、生成完整素材包或未来移动/归档文件时，系统显示具体目标、影响和恢复方式。

**Why this priority**：安全边界必须存在，但不能破坏用户要求的低摩擦体验。

**Independent Test**：对每类 command 执行允许、拒绝、过期、重放、越权与参数替换测试，统计弹窗和实际副作用。

**Acceptance Scenarios**：

1. **Given** Agent 只读搜索和建立未导出的 draft，**When** 连续执行，**Then** 不为每一步弹出权限确认。
2. **Given** Agent 请求导出，**When** 用户确认，**Then** 确认绑定准确项目、revision、目标目录、文件名、覆盖策略和一次操作。
3. **Given** 已确认请求的参数被替换或重放，**When** Core 校验，**Then** 操作失败且没有文件副作用。
4. **Given** Agent 请求任意 shell、raw SQL 或未校验 FFmpeg，**When** 调用，**Then** contract 中不存在该能力或 Core 明确拒绝。

### User Story 6：开放项目包可读、可迁移、可验证（Priority: P1）

用户的项目不依赖某次聊天会话。项目包保存 Creative Blueprint、脚本、字幕、Timeline、operation history、素材引用、Wiki binding、预览/导出引用和 schema version；原素材通过 hash/稳定 ID 引用，默认不复制进项目包。

**Why this priority**：长期项目必须跨 Agent、App 版本和目录迁移继续工作。

**Independent Test**：把项目包复制到干净环境，在原 Library 可用、已移动和不可用三种条件下运行 validator、migration 和 relink。

**Acceptance Scenarios**：

1. **Given** 原 Library 可访问，**When** 打开项目包，**Then** 所有 asset/span 引用按 identity 解析，Timeline 可验证。
2. **Given** 原件移动但 hash 不变，**When** 用户重新连接 Library，**Then** relink 形成显式 operation，项目历史不被重写。
3. **Given** 原件不可用，**When** 打开项目，**Then** Blueprint、脚本、字幕、历史与引用仍可读，缺失素材明确标记。
4. **Given** 新版 App 打开旧 schema，**When** 迁移，**Then** 先备份、验证、生成迁移报告；失败不损坏原包。

## Edge Cases

- Agent CLI 版本比 Core 新或旧；Agent 不理解某个新增 command/result type。
- App 未运行、Core 未启动、sidecar 崩溃或只读安全模式。
- 用户只粘贴文稿但没有足够素材；或素材索引只完成一部分。
- Agent 的自然语言 edit 无法唯一映射为 typed operation。
- 对话与 UI 在同一 revision 上并发修改。
- operation 成功但响应丢失，Agent 使用相同 idempotency key 重试。
- 一个 operation 改变 Blueprint 后使已有 Timeline evidence 失效。
- 用户 undo 后又 redo，再从更早 revision 分叉。
- 项目包复制到大小写规则不同、Unicode normalization 不同的文件系统。
- Agent 在项目文稿、参考字幕或文件名中遇到 prompt injection。
- 外部 Agent 支持图片但不支持音频/视频，或能力在会话中变化。
- 用户授权导出后，目标目录、文件或 permission fingerprint 变化。
- 项目包 schema 可读，但所需 render feature 在当前版本不支持。

## Requirements

### Functional Requirements

#### Agent 无关入口与冷启动

- **FR-001**：MemoLens 首路径不得要求内置聊天、独立模型账户或额外 provider API key；语义与创意由当前兼容 Agent 提供。
- **FR-002**：CLI/Core contract 必须独立于 Codex、DeepSeek Harness 或特定 Agent SDK；Agent-specific Skill/MCP/plugin 只能是薄适配。
- **FR-003**：首次连接 Library 的用户意图可通过 Agent 发起，但实际目录 authority 必须绑定用户批准的准确目录和稳定 Library identity。
- **FR-004**：连接后必须返回媒体规模、基础可用状态、各分析阶段、预计时间/未知和可立即开始的范围。
- **FR-005**：Agent、App 和 CLI 必须读取同一 Library、operation、project 和 revision，不得创建 surface-specific 真源。
- **FR-006**：Core 不可用时必须返回可恢复诊断；CLI 不得退回直接写 SQLite 或项目文件。

#### Creative Blueprint 与灵活流程

- **FR-007**：所有对话创作决定必须编译为版本化 Creative Blueprint；聊天 transcript 不是项目真源。
- **FR-008**：Blueprint 至少表达目标/立场、受众/平台、主题、脚本/口播块、情绪/节奏、参考、技巧选择、素材约束、匹配关系、assumptions、missing evidence 和当前状态。
- **FR-009**：系统必须支持从任意已知输入开始：发布意图、完整/部分文稿、指定素材、参考作品或已有项目 revision。
- **FR-010**：系统必须允许直接生成完整 draft/first cut，不在选题、脚本或技巧处默认阻塞；用户也可显式设置 checkpoint。
- **FR-011**：Agent 的自然语言输出必须经过 schema validation、evidence resolution 和 typed operation compilation，不能直接覆盖 Blueprint/Timeline JSON。
- **FR-012**：改变 platform、tone、aspect ratio 等创作字段不得静默改变事实检索 hard constraints；字段分流遵守 Spec 011 Context Compiler。
- **FR-013**：项目必须固定 Creator Memory、Media Wiki generation、analysis/evidence、Blueprint 和 Timeline revision；current global state 更新不改写旧项目。

#### 统一操作历史

- **FR-014**：所有有状态改变必须形成不可变 operation，至少记录 actor、origin surface、intent、typed command、precondition、input/output digest、timestamp、result 和 affected revisions。
- **FR-015**：App、CLI、MCP 和 Agent adapter 必须调用同一 command handler；不得分别实现业务写逻辑。
- **FR-016**：每个写 command 必须支持 idempotency identity 和 optimistic concurrency；旧 revision 提交不得 last-write-wins。
- **FR-017**：Undo、redo、restore、branch 和 relink 必须通过新 operation/revision 表达，不删除历史。
- **FR-018**：一个自然语言请求若编译为多个 typed operations，必须显示可读摘要；全部操作的原子性或部分成功语义必须预先定义并可重放。
- **FR-019**：无法唯一编译的指令必须返回 ambiguity/options，不得猜测高影响 edit。
- **FR-020**：任何 operation 重放必须在相同 pinned inputs 上产生同等领域结果；不可避免的环境差异必须进入 manifest。

#### 对话与可视化工作台

- **FR-021**：对话和可视化工作台必须共享一个 project head，并实时显示对方提交的 revision/diff。
- **FR-022**：工作台至少可查看/编辑 Blueprint、素材 evidence、Storyboard、Timeline、字幕、Preview、版本历史与 export 状态。
- **FR-023**：Agent 的每个候选和 timeline edit 必须能在工作台定位到受影响的 script block、asset/span 和 operation。
- **FR-024**：用户手工编辑无需复杂“保护区”即可保留；AI 后续修改必须基于 current revision 并通过 typed diff，不得隐式重建整个 Timeline。
- **FR-025**：用户可以从任何 revision 继续对话，不要求把完整历史聊天重新放入 Agent context。

#### Open Project Format

- **FR-026**：项目格式必须有显式 schema version、project identity、content digests 和 migration history。
- **FR-027**：项目逻辑内容至少包含 Blueprint revisions、script/subtitle snapshots、Timeline revisions、operation log、evidence refs、Wiki/Creator bindings、open decisions、preview/export refs 和 validation status。
- **FR-028**：项目默认通过稳定 asset/span/hash 引用原件，不复制、不移动 Library 原文件。
- **FR-029**：项目包必须可由普通文本/JSON 工具检查关键内容；二进制 cache 和临时媒体不得成为理解项目的唯一来源。
- **FR-030**：项目 validator 必须能在不渲染、不调用模型、不改写项目的情况下检查 schema、digest、revision chain、evidence refs 和 feature support。
- **FR-031**：Migration 必须有备份、precondition、checksum、报告、失败恢复和 N-1 兼容策略；不得原地破坏未知扩展字段。
- **FR-032**：跨 Agent resume 必须依赖项目 summary/open decisions 和结构化状态，不依赖导出特定聊天平台 transcript。

#### 权限与不可信输入

- **FR-033**：每个 command 必须声明 effect class：read-only、reversible project write、render/cache write、user export、file-management proposal 或 high-impact file action 的语义等价分类。
- **FR-034**：普通 read 和可撤销 project edit 应低摩擦；user export、overwrite、完整素材包和未来文件移动必须使用绑定准确资源与参数的短期确认/capability。
- **FR-035**：Agent 不得获得 raw SQL、任意 shell、任意绝对输出路径、自由 FFmpeg filter graph 或 capability 签发能力。
- **FR-036**：项目文本、素材 OCR/ASR、Wiki 页面、网络研究与 Agent 输出都按不可信数据处理，不能成为提权指令。
- **FR-037**：Agent capability discovery 必须明确图片、视频、音频、网络搜索、最大 payload 和可写 command；缺失能力必须降级或报告，不伪装已分析。
- **FR-038**：Agent 分析写回必须绑定 analysis request、input manifest、output schema 和 model/Agent identity，不能提交无来源自由文本成为素材事实。

### Key Entities

- **Agent Adapter**：把某个 Agent host 的调用转换为统一 Core contract 的薄层。
- **Core Command**：带 effect class、precondition、idempotency 和稳定 result 的领域动作。
- **Creative Project**：跨 Agent、UI 和版本持续存在的创作容器。
- **Creative Blueprint Revision**：当前项目表达、脚本、参考、技巧、约束和素材计划的不可变快照。
- **Script Block / Beat**：文稿、口播、情绪或叙事作用的可引用单位。
- **Project Binding**：项目固定的 Creator/Wiki/analysis/evidence/Timeline revision 集。
- **Operation**：一次有意义且可审计的项目状态变化。
- **Branch / Head**：operation/revision 链上的可继续编辑位置。
- **Open Decision**：Agent 恢复项目时需要知道的未决选择或 gap。
- **Effect Class**：command 对状态/文件的影响等级。
- **Scoped Capability / Confirmation**：用户对一次准确高影响动作的短期授权。
- **Project Package**：开放、版本化、可验证的项目逻辑内容及可选衍生制品。
- **Capability Profile**：当前 Agent 和本机可用的视觉、音频、搜索、渲染与写入能力快照。

## Success Criteria

### Measurable Outcomes

- **SC-001**：Codex 和 DeepSeek Harness 各自在没有额外 MemoLens 模型 key 的条件下完成同一黄金旅程：连接 Library → 搜索证据 → 创建 Blueprint → first cut → 一次对话修订 → 一次 UI 修订 → preview；完成率 100%。
- **SC-002**：在干净安装可用性测试中，至少 90% 目标用户只需作出一个概念性设置决定（选择 Library）即可看到索引开始；没有用户被要求输入 database、Python、FFmpeg 或 provider 配置。
- **SC-003**：直接 first cut 路径与逐步共创路径产生符合相同 Blueprint/Timeline contract 的项目比例 100%，且任一路径都可继续使用另一种交互深度。
- **SC-004**：交替执行至少 100 个 Agent/UI typed operations 后，operation replay 的 Blueprint/Timeline canonical digest 一致率 100%；未记录状态改变为 0。
- **SC-005**：undo/redo/restore/branch 覆盖全部支持的 project mutation；随机 200 条操作链恢复目标 revision 的成功率 100%。
- **SC-006**：并发旧 revision 写入静默覆盖次数为 0；所有冲突返回稳定 diff 与可恢复下一步。
- **SC-007**：Agent A 创建项目、Agent B 无聊天历史恢复后，B 正确说出 current Blueprint、Timeline、open decisions 和 evidence coverage 的人工审核准确率 ≥95%。
- **SC-008**：相同 pinned project/query 经两个 Agent 执行时，Core candidate IDs、hard constraint verdict、command precondition 和 committed state 一致率 100%。
- **SC-009**：项目包 validator 在不调用模型和不修改文件时识别所有预设 schema/digest/revision/evidence 损坏，漏报为 0，误报低于 1%。
- **SC-010**：N-1 项目迁移成功率 100%；任一注入失败后原包 byte/digest 不变，迁移报告完整。
- **SC-011**：安全矩阵中，未经确认的 export/overwrite/full package/file move 成功数为 0；普通 read-only 查询的额外确认弹窗为 0。
- **SC-012**：所有 Agent semantic write-back 都能追溯到 request/input payload/model/output；无来源 observation 成为 current 的次数为 0。
- **SC-013**：相对“只有工作台表单”的 baseline，用户从意图到首个可播放 first cut 的中位主动操作数降低至少 50%，且对最终可控性的评分不下降。

## Rollback and Degradation

- 先让新 CLI 只读并对照现有 plugin；写 command 逐类晋级，不一次开放全部能力。
- 某个 Agent adapter 失败时，项目仍可由 App 或另一个 Agent 打开；adapter state 不写入项目真源。
- Semantic Agent 不可用时，用户可继续打开项目、手工编辑 Timeline、验证和渲染已有 revision。
- 新 project schema 未通过迁移门槛时继续读取旧 schema，不原地升级。
- Operation compiler 无法解释自然语言时退回候选 typed ops/手工工作台，不执行自由指令。
- High-impact capability 失效时只阻止对应动作，不损坏 draft、preview 或历史。

## Out of Scope

- 不自建聊天模型、Agent scheduler、multi-agent orchestration 或通用 autonomous runtime。
- 不要求保存、同步或导入 Codex/DeepSeek Harness 的完整聊天记录。
- 不让 CLI 成为另一个业务内核或直接 DB client。
- 不在本规范定义完整专业 NLE 功能、云协作、CRDT 或社交平台自动发布。
- 不以“一键全自动”为由取消 Blueprint、evidence、history 或工作台。
- 不要求所有 Agent 具有相同多模态能力；要求的是诚实 capability 与稳定 contract。

## Assumptions

- 用户已拥有至少一个能调用本地 CLI/工具的兼容 Agent；首批目标是 Codex 和 DeepSeek Harness。
- Codex/DeepSeek 共用 Browser Canonical Editor 是主剪辑面；MemoLens 桌面 App/Electron 继续提供原生目录/导出确认、本地 runtime/broker 和兼容 reader。
- Core 可以在 App、受控 sidecar 或未来本地服务中承载统一 command；具体进程拓扑留给 plan。
- Spec 014 提供 Agent 可导航的素材 evidence；Spec 005 提供 Timeline/Preview 基线。
- “直接 first cut”仍受 evidence、素材 coverage 和当前 render capability 约束。

## Dependencies

- [ML-004](../004-evidence-backed-retrieval-privacy-benchmark/spec.md)：黄金旅程、用户结果与隐私基线。
- [ML-005](../005-video-creative-workbench.md)：现有 brief、Timeline、typed diff、validation、preview/export 基础。
- [ML-007](../007-local-capability-boundary/spec.md)：root、runtime、Agent payload 和高影响操作边界。
- [ML-008](../008-unified-media-memory-kernel/spec.md)：统一 identity、operation 和 revision。
- [ML-011](../011-consentful-creator-model/spec.md)：Creator context 分流与 confirmed-only 规则。
- [ML-014](../014-agent-navigable-media-wiki/spec.md)：Agent 搜索、evidence 和 project memory binding。
- [ML-016](../016-craft-wiki-technique-compiler/spec.md)：专业技巧知识与 Blueprint 编译。
- [ML-017](../017-export-usage-ledger-material-package/spec.md)：导出 usage ledger 和项目素材包。
- [ML-018](../018-script-coverage-global-footage-assignment/spec.md)：文稿/口播的全片级 coverage、素材分配与局部重算。

## Authorization Boundary

用户已授权将 Grill Me 共识按小切片实现。ML-015-A0/A1/B0/B1 均已实现并通过独立验证；ML-015-B1A 作为不改变 V5 schema/capability 边界的 maintenance 切片，已实现并通过本地验证。B1 在 B0 canonical unverified proposal ledger 之上交付 V5 additive capability/authority ledger、经 Desktop 原生批准的短期项目级 paired CLI proposal commit/restore、main-only 8-unit confirm/revoke 与 current/exact/historical authority projection；B1A 只收紧 authority deadline、credential read、SQLite writer admission/verified-prefix、backend lifecycle、health 与 strict JSON 边界，不新增任何创作或文件能力。Pairing 只授予可恢复的 proposal write，不证明厂商身份或用户认可。

[ML-015-A2 Plugin-First Library Bootstrap](slices/015-a2-plugin-first-library-bootstrap/spec.md) 已进入实施，但当前严格限于 `PHASE 0-1`：Agent-neutral request/status 和 Electron broker-only native chooser 边界。在该切片独立证据升级前，不声称 Core 已采纳 Library、indexing 已开始、Blueprint 已 grounded 或 editable Timeline 已存在。

当前共享工作树已实现 ML-018-A1 deterministic baseline Coverage、ML-015-B2B revision-1 lowerer、B2B2 V9 successor/history/inspection、B2B3 V10 canonical manual basic edits/save-then-reread，以及 ML-017-A/A2/A3 canonical export、Usage/residual/derivative 链。B2B4 已实现 V11 paired `timeline.apply_edit`、V12 versioned generic receipt convergence、Codex/DeepSeek 共用 Browser Canonical Editor 与 permanent paired Timeline receipt；B2B4A 增加 verified visual replacement，B2C0 以 V13 增加只读 K 与独立 `timeline.restore_revision` 的 K→N+1 append-only restore，B2B4C 以 Timeline v2/V18 增加独立授权的 video Split 与 Remove from Timeline（不删原始媒体）。这些可见剪辑都在 Browser Canonical Editor；Electron 仅作 native authority/runtime/broker 与兼容 reader。

[B2B4B](slices/015-b2b4b-safe-canonical-playback-grant/spec.md) 交付 project/head/clip-bound source preview；[B2B4B.1](slices/015-b2b4b1-preview-transport-audio-truth/implementation-evidence.md) 将真值修正为“raw MP4 transport 可能含音频，页面禁止 audio playback 并保持 output muted，不证明 audio stream/content/mix/final fidelity”。controlled-local Codex Browser 已视觉播放 AAC fixture，但 DeepSeek 只完成 fresh loader/plugin inventory，两端均未完成真实 model-driven 双向 T053/T054，也未运行 native user/audio-device 验收。B2C 跨资源 unified project history、source-audio edit/mix、subtitle/audio-level、final-fidelity preview、Wiki/Technique/research pin、新项目冷启动、完整素材包、Remote CI 与 release 仍未完成。

0.10.0 仓库级 `npm run check` 通过 Ruff、218 项 backend/Core Python、223 项 plugin、local deployment verify、renderer/Electron build、71 项 Node 与 46 项 renderer-model。独立 B1 复核复现 commit-only/restore-heavy 10/50/100 公共投影常量斜率，结论为 `P0=0 / P1=0 / Go`；长期写入/capability 验证、近上限原生审阅、fetch timeout、credential bounded-open 与 MCP 内层 schema 的非阻断 P2 按 [B1 实施证据](slices/015-b1-paired-agent-decision-authority/spec.md#implementation-evidence) 保留。

0.10.1 maintenance candidate 的 [B1A 实施证据](slices/015-b1a-authority-runtime-hardening/implementation-evidence.md) 录入当前本地结果：B1A focused 42 项、B1 相关 78 项、Blueprint API/contract/state-machine 28 项与 plugin 228 项通过，独立复审为 `P0=0 / P1=0 / LOCAL GO`；最终 diff 上两轮完整 `npm run check` 均通过 Core/unit 289、plugin 228、Node combined 86、renderer models 46，dependency audit 为 0 个已知漏洞。GitHub-hosted CI 未运行，因此仍不声明 0.10.1 已发布或 hosted validated。
