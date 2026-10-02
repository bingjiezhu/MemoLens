# MemoLens Spec ↔ 实现决策复核

> **2026-08-22 产品决策补充**：本文件的代码审计、安全缺口、canonical ledger、可恢复任务、typed evidence 与发布门槛继续有效；产品入口和长期方向以 [43 问产品对齐记录](product-decisions-43-questions-2026-08-22.md) 及 [ML-014–018 路线图](roadmap.md) 为准。具体更新包括：从 Codex 单一表面扩展为 Agent 无关 CLI/Core；不内置聊天或第二个模型 key；语义理解可使用当前外部 Agent 的受限多模态能力；checkpoint 可由用户选择而非固定阻塞；成功导出即记录精确 usage；新增 Media Wiki、Craft Wiki 与全片 Script Coverage。二者冲突时按该范围采用较新决策。

- 决策日期：2026-08-20
- 决策状态：DECIDED
- 代码实施授权：NONE
- 适用范围：Spec 004、005–013 的组合取舍与实施顺序
- 证据截止：2026-08-20 当前工作树

## 1. 最终决策

MemoLens 下一步不应继续堆叠「AI 能力」，而应完成一个可信单一素材库闭环：

~~~text
安全选择一个 Library
  → 照片与视频只导入一次
  → 后台可恢复地形成同一份资产记忆
  → App 与 Codex 用同一语义找到真实素材
  → 清楚说明为什么选中、为什么没找到
  → 只在正确阶段应用用户确认过的偏好
  → 生成可编辑、可回退、来源有效的 first cut
~~~

八份新 Spec 的方向没有整体跑偏，但它们合计 252 条 Functional Requirements 和 99 条 Success Criteria，不能直接当成一个迭代 backlog。本决策把它们分为「必须」「有证据后做」和「现在不做」。

## 2. 产品不可退回原则

| 原则 | 当前产品与代码证据 | 对 Spec 的约束 |
| --- | --- | --- |
| 私人、本地优先，外发必须可见 | [product-strategy.md](../product-strategy.md) 定义 payload disclosure 和 local fallback；视频路径已默认禁止外发 | 必须修复已知越权与外发链；不把「本地优先」曲解成牺牲效果的绝对断网 |
| 原件不动，整理可撤销 | Inbox 的 Keep / Archive / Undo 和 Creator revision 已有回归测试 | 新内核必须保留不可变用户决定；projection 可重建，用户事实不可删 |
| 结果落到真实素材和时间范围 | Video Workbench 已保留 evidence、segment range 和 analysis revision | 诚实空结果和证据追溯是核心；不用无证据候选填满 top-k |
| 用户要可编辑初剪，不是黑盒成片 | Timeline revision、typed diff、validation 和 preview 已存在 | 进化现有 brief/timeline，不另建不可控 Agent 或全新创作平台 |
| 个性化只有确认后生效 | Creator Memory 只应用确认 revision | 保留显式确认；不默认从停留、点击、搜索和沉默学习 |
| App/Core 掌管状态与确认，外部 Agent 是对话表面 | plugin 默认只读同一 SQLite，App 提供 Continue in Codex | 继续不新增第二套记忆、聊天或通用 Agent runtime；Codex/Claude 等通过同一受控 contract |
| 低摩擦，每次一个下一步 | 主界面已把运行时与模型放入 Advanced | 普通本地操作不应每次弹授权；高影响操作才要原生确认 |

历史文档中的「个人管理 Agent」与现行产品策略存在冲突。本轮裁决是：保留 agentic experience，不建设通用 Agent 基础设施。用户仍可用一句话完成多步检索与创作，但每一步由受约束意图、真实证据和确定性操作执行。

## 3. 当前实现对照后的必做与带条件切片

### P0-1：关闭已证实的能力越界和外发链

这是已存在的安全缺口，不需要等待完整 capability 平台或 benchmark 才修复。

- Renderer 可保存任意 pythonCommand，main 后续直接执行：electron/desktopSettings.ts、electron/main.ts、electron/backendProcessSupervisor.ts。
- Legacy /v1/indexing/jobs 只检查 loopback，接受 image_dir/files/db_path；absolute file 可直接进入处理，配置远程 VLM 时又会发送 resized image bytes：backend/src/api/routes.py:887、core/schemas.py:364、indexing/pipeline.py:462、indexing/vision.py:54。
- Photon 仅做 lexical containment，内部 symlink 可跟随到根目录外；转码失败又回退上传原文件：photon-bot/src/imageResolver.ts、photon-bot/src/discord.ts。
- Render 和 video analysis 存在「校验路径后再由 FFmpeg 重开」的 check-use 窗口：backend/src/media/render_sources.py:34、backend/src/media/render.py:305、backend/src/media/video.py:738。

决策：实施 007-A。Production 只启动受信 runtime manifest；root/DB 只来自 main-owned 选择；legacy indexing 不再接受 raw path/DB authority；原件读取、provider 外发和导出绑定稳定 source identity；Photon 只发送重新解码、重新编码的安全副本，任一步失败都 fail closed。普通 Inbox/Timeline 修改不增加额外弹窗。

产品兼容性裁决：手动 browser fallback 降为 dev/diagnostic-only，不是 production authority surface。开发者可在启动 backend 前使用明确环境配置，或以后使用独立 local-admin CLI 注册测试 root/DB；普通 browser session 和网页 Control 不得签发、扩大或持久化 production library/database authority。Electron production 仍由 main-owned 原生选择掌权。

### P0-2：立即停止隐藏工作台的无意义工作

src/App.tsx 在 Create 路由中始终挂载 VideoWorkbench，只用 hidden 切换 photo/video；工作台内部会继续轮询 job 和 render 状态。

决策：这不需要新 Spec，列入第一个可用性修复。非 active 模式必须卸载或显式 suspend，隐藏后不再请求 capabilities、job 和 render poll；已提交的 durable job 仍由 backend 继续，UI 只停止无效订阅。

### P0-3：用一个 canonical media ledger 结束照片/视频分裂

Photo Create 使用 /v1/retrieval/query，Video Workbench 使用 /v1/search/mixed，Atlas 使用第三套表和索引。Legacy 照片先写 image_index、再单独同步 media asset；新 media import 的照片又不一定进入 legacy Search/Atlas。同一素材因此可能在一个页面可见、在另一个页面不可见。

决策：实施 008-A，这是最高优先级的架构收敛。把现有视频链已验证的 Asset/Source/Analysis Revision/durable job/idempotency 规则扩展给照片，再 shadow 生成 legacy projection。产品验收单位是：

> 导入同一素材 → Inbox、Search、Memories、Create 可见 → Codex 读取同一 asset ID 和 evidence revision。

禁止一次性换表或先大规模搬目录。新老路径必须并行对照，保留差异工件和回退开关。

### P0-4：照片分析必须成为可恢复后台任务

/v1/indexing/jobs 当前在 HTTP 请求内串行完成 EXIF、geocode、VLM、embedding、quality 和双写；「job」未持久化，源文件在 hash 后也会多次按路径重开。

决策：纳入 008-A。Create 返回 202 + 稳定 operation ID；任务固定 library/database/source identity，按阶段 checkpoint，支持 cancel/restart/resume，只在来源重验通过后原子 publish current analysis。

### P0-5：Embedding 和 Atlas 必须停止给用户错误语义

默认 semantic_hash 的 image embedding 实际不读像素，只哈希描述/文件名；Atlas 却把它当视觉和重复信号。Atlas 还会补零/截断混合不同向量空间，并构建全量 N×N 相似度矩阵。

决策：实施 008-B，但在 008-A canonical bridge 后切换。semantic_hash 必须明确标为 text-derived fallback，不参与 visual duplicate 声明；向量带完整 space/model/preprocess provenance；不同 space 不直接比较；Atlas 改为增量邻域/projection，展示布局不再被当作语义真源。

### P1-1：合并现有检索优点，不建第四套检索器

Legacy Photo planner 已有日期、地点、required/optional/excluded 结构；mixed media 路径却明确返回 lexical_local_fallback 且 semantic_available=False。

决策：在 008-A 稳定后实施 009-A：一个 deterministic typed intent、一个 image/video candidate contract、一个 evidence/reason/error DTO。先支持已有真实证据的 text/media type/time/location/include/exclude/orientation/duration/count/order；返回 satisfied、unknown、excluded 和 unsupported；区分 no evidence、not indexed、source unavailable、permission denied 与 constraint conflict。LLM planner、图检索和远程慢路径只在 shadow benchmark 证明必要后晋级。

### P1-2：修复 Creator Memory 字段污染检索

Photo Create 当前把 platform、duration、aspect ratio、tone、pace、narrative arc 与 include/exclude 全部拼进检索 prompt；仅把 1:1 改成 9:16 都可能改变事实候选集。Video Director 已展示正确边界：创作字段留在 brief，must include/exclude 才进入检索条件。

决策：实施 011-A Context Compiler，不先做主动学习。把上下文编译为四个分离 channel：

- Retrieval：内容必含、内容排除、明确主题。
- Director/ranking：叙事、节奏、受众。
- Copy：语气、平台文案。
- Render：画幅、时长、输出格式。

核心验收是 metamorphic test：改变画幅、platform 或 tone 不改变事实候选；改变 must-exclude 只影响对应 retrieval clause；project override 不升级为长期偏好。

### P1-3：Reset 不能冒充完整遗忘

当前 Creator Reset 通过新增空 revision 让偏好不再生效，但旧 preference value/evidence 仍存在。这适合作为可审计 Reset，不等于「删除我的偏好数据」。

决策：011 的后续必做切片要区分 Reset 与 Full Forget。Full Forget 删除 MemoLens 控制范围内的 active value、evidence、projection/cache 和 learning reference，只保留不含原值的 opaque tombstone；已导出或已经授权外发的副本必须如实标记为不可召回。它不阻塞 011-A Context Compiler，但在产品提供「遗忘」承诺前是发布门槛。

### P1-4：在自由文案和 publish-ready 之间增加最小证据门

现有 Timeline 已是良好的确定性编译器，但照片 caption/copy 和 story 文字没有最小 claim → evidence span 合同。

决策：012-A 只在现有 brief/storyboard 上增加 factual / interpretive / user-provided / stylistic 分类、evidence、selection reason、missing material 和 immutable revision。无 evidence 的 factual claim 保持 review-required。暂不实现完整 Story Graph 或 C2PA。

### P0-6：从「源码能跑」变成「普通人可以安装」

当前主流路径仍要求源码、Node、Python、FFmpeg 和 Homebrew，没有正式 packager、single-instance 与完整 sidecar lease。这与独立创作者桌面产品定位直接冲突。

决策：

- 013-A，下一迭代必须：single instance、backend identity challenge/process lease、有界退出、orphan cleanup、crash budget、只读安全模式，并与 007-A 的受信 runtime 连接。
- 013-B，公开 beta 前必须：macOS 13+ Apple silicon arm64 的签名、公证、自包含 .dmg，内置 backend/runtime/FFmpeg，干净机器断网仍可完成核心旅程。
- 013-C/D，GA 或有更新通道时必须：N-1 迁移矩阵、签名更新、回滚、SBOM 和 build provenance。

Intel x86_64 不是首发必备。只在有明确用户需求和维护能力后声明支持；一旦声明，必须通过同等签名、干净安装、迁移和生命周期门槛。

## 4. Spec 组合决策

### 决策标签

- MUST-NOW：已有明确代码证据和用户伤害，下一个实施周期必须进入。
- MUST-STAGED：与产品理念必然一致，但必须分纵向切片交付，不允许大爆炸重构。
- SHOULD-EXPERIMENT：方向契合，但要先证明能减少用户时间或编辑操作。
- NOT-NOW：不进入已承诺路线，只保留为研究选项。

| Spec | 组合决策 | 必须实施的最小切片 | 延后/删除出已承诺范围 |
| --- | --- | --- | --- |
| 004 | MUST-NOW / LITE | 004-A：小型 golden journey、privacy network deny、当前 baseline 与差异工件 | 完整研究数据集、公开 claims registry、复杂删除闭包按真实发布节点分期 |
| 005 | PARTIALLY IMPLEMENTED | 保住 grounded brief、segment evidence、timeline revision/diff/validation/preview | 1080p final export 尚无 output grant，不得标记全量完成 |
| 006 | IMPLEMENTED / NOT VALIDATED | 保住 reversible Inbox 和 confirmed-only Creator Memory | 在 004-A 产品旅程验证前不升级为 VALIDATED |
| 007 | MUST-NOW / CORE | 007-A：runtime、legacy indexing、root/DB authority、原件读取、Photon/Provider 外发 | 不先建通用 token service/policy DSL；007-C 只在新远程媒体能力上线时做 |
| 008 | MUST-STAGED，最高架构优先级 | 008-A canonical photo bridge + durable job；008-B versioned embedding/projection | 全量历史迁移与 legacy retirement 在 shadow 差异清零后做；standalone audio 不进 P0 |
| 009 | MUST-STAGED / CORE | 009-A deterministic typed intent、honest empty、reason/error DTO、image/video 同一 contract | source federation、LLM planner、fast/slow 远程路由必须通过 paired shadow experiment |
| 010 | SHOULD-EXPERIMENT | 先提高精确视频时段与可修正 event membership，放入 Memories | full event/episode/concept graph、人物关系推断、「第一次/永远」绝对判断 |
| 011 | MUST-STAGED / 011-A Context Compiler | 在 008-A/009-A 后把 confirmed profile + project override + task intent 编译到四个 channel；提供遗忘承诺前补 Full Forget | 默认主动学习、行为 hypothesis、opaque personalization adapter |
| 012 | SHOULD-EXPERIMENT | 在现有 brief/storyboard 上增加四类最小 claim enum、evidence、reason、missing material 和 immutable revision | 完整 claim ontology、独立标注体系、Story Graph、C2PA/自动 publish-ready 不进当前主流 |
| 013 | MUST-STAGED | 013-A 运行时生命周期；013-B arm64 clean signed bundle | 无用户证据不强制 x86_64；auto-update、双 runner reproducibility 和完整供应链矩阵分期 |

## 5. 修正后的依赖与执行顺序

Spec 007 的原始版本曾要求 008 提供稳定 identity，008 又把 007 列为前置，形成事实循环；该循环已按本决策修正。Spec 004 也不应成为已知安全修复的阻塞项。切片级依赖如下：

~~~mermaid
flowchart LR
    UI["Immediate: hidden polling fix"]
    A004["004-A golden/privacy baseline"]
    A007["007-A authority containment"]
    A013["013-A instance + sidecar lifecycle"]
    B013["013-B arm64 clean signed bundle"]
    A008["008-A canonical photo + durable jobs"]
    B008["008-B embedding + projections"]
    A009["009-A typed honest retrieval"]
    A011["011-A context compiler"]
    A010["010-A temporal refinement experiment"]
    A012["012-A evidence story experiment"]

    A007 --> A013 --> B013
    A007 --> A008 --> A009 --> A011
    A008 --> B008
    A009 --> A010
    A011 --> A012
    A009 --> A012
    A004 -. continuous evidence gate .-> A007
    A004 -. continuous evidence gate .-> A008
    A004 -. promotion gate .-> A010
    A004 -. promotion gate .-> A012
~~~

1. 立即开始 007-A 和 hidden polling 修复；004-A 同步冻结当前行为，但不阻塞已知安全修复。
2. 007-A 先用当前 database UUID/root manifest 完成最小权限收缩，不依赖 008。
3. 008-A 在可对照纵向切片内引入 canonical identity；007-B 与 008-B 再共同收敛最终 capability/identity contract。
4. 013-A 可在 008-A 之前独立交付；013-B 是公开 beta 门禁，不需要等待完整 008-C。
5. 009-A 不等待完整 010 event graph；未提供的 event/relationship clause 返回 unsupported/not-indexed。
6. 012-A 只依赖现有 confirmed Creator revision 和 011-A，不被主动学习阻塞。

## 6. 004-A 必须冻结的六条产品生命线

1. 干净 Apple silicon Mac 从安装到第一项可检索素材，无终端和开发依赖。
2. 同一素材在 Inbox、Search、Memories、Create 和 Codex 中 identity 一致。
3. 「去年海边，不要自拍」正确执行时间与排除条件，违反 hard constraint 的结果为零。
4. 没有证据时诚实返回空状态，并指出等待索引、恢复权限或放宽条件哪个有意义。
5. 修改 9:16、platform 或 tone 不改变事实候选；修改 must-exclude 才改变对应候选。
6. 索引或渲染中途退出，重启后回到可解释终态，无半写 revision 和孤儿进程。

跨越全程的硬门槛：原始媒体内容不变；offline profile 的非 loopback 网络请求为零。

004-A 的第一份报告允许把尚未实现的旅程明确记录为 red/unavailable；它的职责是冻结真实现状，而不是制造全绿结果。007-A、008-A、009-A、011-A、013-A/B 只有在各自对应生命线转绿后才能晋级或发布。

## 7. 已经做对的部分，重构不得退回

- MediaRepository 的 immutable analysis/head、revision/CAS 和事务幂等。
- 新媒体 import 的 source identity、symlink/TOCTOU 防线和受限 root。
- Video 默认不外发、本地 FFmpeg 流水线和有时间范围的 evidence。
- Creator Memory 的 confirmed-only、不可变 revision、显式 scope 和可撤销。
- Timeline 的 typed revision/diff/validation 与绑定渲染输入。
- Inbox Archive 不移动原文件，并能撤销。
- Codex plugin 默认只读、使用同一 SQLite，不成为第二个权威写入者。
- 模型可以提出结构化意图和时间线操作，但不能执行自由 shell 或未验证 FFmpeg 参数。

## 8. 明确 NOT NOW

- 通用 Agent runtime、自由工具调用、多 Agent 调度和第二套聊天/记忆。
- 微服务、Redis/Celery、独立图数据库、大型向量服务和新消息队列。
- 默认从停留、点击、搜索、沉默或原始 prompt 推断长期偏好。
- Inbox 连续主动提问、opaque personalization adapter 和无法解释的个性化排序。
- 全库人物关系、生物识别或个人知识图，以及默认 full event/concept graph。
- C2PA/Content Credentials 作为当前主流，以及自动发布社交平台。
- 没有明确用户需求时首发强制 Intel x86_64。
- standalone audio ingest/ASR、云同步、多人协作和云端全库上传。
- 为了「目录看起来漂亮」做大爆炸重构；目录调整只跟随已稳定的 bounded context。

## 9. 研究能力的晋级规则

010、011 的学习部分和 012 只能从 shadow/experiment 晋级为默认功能，当且仅当：

1. 与固定 baseline 做 paired 对照，不用不同难度 bucket 代替因果对照。
2. 主指标是用户结果：从搜索到加入 timeline 的时间、找到正确视频时刻的成功率、修订次数、错误候选率和恢复成功率。
3. 同时报告 answer coverage/risk-coverage，不能用全部拒答刷高 precision。
4. 延迟、内存、隐私、简单查询质量和非个性化用户有非劣 guardrail。
5. 未达门槛时终止或降级，不用「以后数据多了会更好」解释失败。

## 10. 证据范围与限制

本决策使用了：

- 当前可读取的 MemoLens 对话，包括本轮架构审计、前沿调研、Spec 编写与终审。
- 仓库的 README.md、docs/product-strategy.md、历史项目说明、迭代记录与 Git 历史。
- 当前 Electron、React、Flask、indexing、media DB、retrieval、Atlas、Creator Memory、render、Photon 和测试实现。

当前 Codex 历史中可检索到的 MemoLens 任务只有当前任务；本文没有伪称看过不可读取的对话。当历史文档与现行产品策略冲突时，以当前可观察产品行为、回归测试和 docs/product-strategy.md 为更高权威。

## 11. 本轮边界

本文是组合决策和后续验收约束，不是实施授权。本轮不修改产品代码、数据库、配置、构建或发布流程。
