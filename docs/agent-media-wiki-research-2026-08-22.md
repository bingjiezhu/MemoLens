# MemoLens Agent 可导航媒体 Wiki：开源、Google 与学术调研

- 调研日期：2026-08-22
- 范围：OriNodes-v4、Google/DeepMind、活跃高星开源项目、2024–2026 顶会论文与少量明确标注的前沿预印本
- 来源原则：优先官方仓库、官方文档、正式论文页与论文正文
- GitHub 热度快照：2026-08-22；Star 会变化，只表示采用热度，不构成技术优越性证据
- 实施授权：`NONE`

## 1. 最终判断

MemoLens 不应被定义为“把视频描述成 JSON，再做向量 RAG 的剪辑器”。更准确、也更有代际差异的目标是：

> **Versioned Temporal Multimodal Evidence Wiki with Agentic Active Retrieval**
> 一个以私人媒体时间段为高保真情景记忆、以类型化时序关系和活 Wiki 为导航、由 Agent 主动搜索与复核、最终服务创作蓝图和可编辑时间线的个人创作者媒体知识系统。

没有发现一个公开项目同时完成以下闭环：

1. 一个不搬动原件的长期私人素材库；
2. 图片和视频精确时段的多模态证据；
3. 已使用区间与剩余可用区间；
4. Agent 可逐层导航并按需回看原素材；
5. 文稿节拍到镜头集合的全局分配；
6. 专业电影知识到可执行剪辑方案的动态编译；
7. 对话、可视化时间线、单步历史和开放项目包；
8. 导出时回写可验证的素材使用事实。

因此，MemoLens 的创新机会不是再造一种 embedding，而是把上述环节组织成一个统一、可验证、可回退的创作操作系统。各外部项目只提供其中一部分。

## 2. 用户记得的 Google 方向是什么

结论是两条相邻但不同的 Google 路线：

### 2.1 Open Knowledge Format：Agent 知识的开放交换格式

[Open Knowledge Format v0.2](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)（OKF）由 Google Cloud 在 2026 年公开。官方仓库 [GoogleCloudPlatform/knowledge-catalog](https://github.com/GoogleCloudPlatform/knowledge-catalog) 在本轮快照约 8,805★，Apache-2.0。

它定义的是一个极简、可读、可 diff、可移植的知识 bundle：

- Markdown + YAML frontmatter；
- 分层目录与每级 `index.md`，让 Agent 先看目录再加载正文；
- `log.md` 记录更新；
- `sources` 表达来源；
- `generated` 与 `verified` 分开表达生成和验证；
- `status`、`stale_after` 表达生命周期与新鲜度；
- Attested Computation 区分“定义被验证”和“某次执行确实按定义完成”。

Google 对 OKF 的定位非常重要：它是格式，不规定存储、服务、查询引擎或领域 taxonomy。[官方说明](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing)也把生产者、可视化消费者和 Knowledge Catalog 接入视为不同实现。

**MemoLens 裁决**：

- 采用 OKF 的渐进披露、来源、验证、新鲜度、生命周期和可移植思想；
- 提供 OKF-compatible 的人类/Agent 可读 Wiki projection；
- 不把 Markdown bundle 当百万级媒体片段的事务数据库；
- 用 MemoLens 扩展表达精确时间范围、类型化关系、analysis revision、sensitivity 和使用区间；
- OKF v0.2 的普通 Markdown link 不带边类型，不能替代内部时序关系 contract。

### 2.2 Google Code Wiki：持续更新、页面导航与精确回链

[Google Code Wiki](https://developers.googleblog.com/introducing-code-wiki-accelerating-your-code-understanding/) 在 2025 年公开预览。它扫描仓库、持续更新结构化 Wiki，把页面和回答链接到具体文件/定义，并计划通过 Gemini CLI extension 支持私有仓库。

这验证了一个产品形态：

```text
底层复杂对象持续变化
  → 自动维护结构化 Wiki
  → 人和 Agent 先从高层理解
  → 再跳回精确原始证据
```

**MemoLens 裁决**：代码定义对应媒体 asset/span，架构图对应事件/主题/作品地图，源码链接对应具体帧、字幕、音频与时间码。MemoLens 要借工作方式，不复制代码领域 schema。

### 2.3 Knowledge Catalog 与 Ask Photos：检索不是一次向量搜索

[Google Cloud Knowledge Catalog](https://cloud.google.com/blog/products/data-analytics/introducing-the-google-cloud-knowledge-catalog) 的三个支柱是 aggregation、continuous enrichment 和 search，并把来源冲突、关系、验证规则和 Agent 检索作为整体问题。它说明“有很多 metadata”不等于 Agent 有可信上下文。

[Ask Photos](https://blog.google/products-and-platforms/products/photos/ask-photos-google-io-2024/) 更接近 MemoLens 的产品先例：理解意图、制定检索计划，结合人物、地点、时间与自然语言概念，再用 Gemini 对候选做多模态判断。[2025 更新](https://blog.google/products-and-platforms/products/photos/updates-ask-photos-search/) 又采用双速体验：普通搜索立即返回，复杂问题继续由 Gemini 处理。

**MemoLens 裁决**：

- 基础 metadata/FTS 先返回；
- 多条件、低置信、文稿配画面和多跳问题进入 Agent 慢路径；
- 大模型负责少量候选的语义复核，不是盲看整个 Library；
- 结果必须比 Ask Photos 多两类领域事实：精确视频 span 和素材使用区间。

## 3. OriNodes-v4 代码架构复核

本轮只读检查的 canonical 版本是 `<local-reference-checkout>`：它是有效 Git 仓库，2026-08-21 最新本地提交为 `31700b97e30e42ee8efc0e52b3fcfb7e94005f7f`；其 AGENTS.md（外部本地参考，未随仓库发布） 明确把该主线声明为 v4 权威版本。

### 3.1 OriNodes 当前真正实现了什么

它不是完整知识图数据库。它更准确的结构是：

```text
Archive 原始证据
  → Case / Insight / Skill Markdown 派生知识
  → Artifact Revision / Review / Snapshot 治理层
  → Agent 小型 context pack
  → 按稳定 evidence ref 精确钻取
```

有价值的实现包括：

- current-architecture.md（外部本地参考，未随仓库发布）：原始、治理、实时 Agent 三层以及 projection 非权威原则；
- evidence.py（外部本地参考，未随仓库发布）：稳定 ID、来源、时间、hash、visibility 和幂等证据写入；
- validation.py（外部本地参考，未随仓库发布）：Wiki frontmatter、证据引用、权限、追加式演进与禁止自动激活；
- wiki.py（外部本地参考，未随仓库发布）：`case → insight → skill` 派生，反例和适用范围变化进入连续更新；
- episode.py（外部本地参考，未随仓库发布）：基于稳定引用而非语义相似度确定 episode 连通身份；
- recall.py（外部本地参考，未随仓库发布）：先召回 Wiki，再提示 evidence drilldown；
- service.py（外部本地参考，未随仓库发布）：不可变 revision、review、snapshot head 和 rollback；
- gateway/cli.py（外部本地参考，未随仓库发布）：Agent 无关的结构化 JSON 工具入口。

当前所谓“图”主要由 `evidence_refs`、`case_refs`、`source_refs`、`episode_key` 和 `supersedes` 形成隐式引用；没有持久化边索引、图数据库或真正的 WikiLink 多跳检索。当前召回仍是文件扫描、字符/词元重叠和固定权重。

### 3.2 应迁移的思想

1. 原始证据永远高于 Wiki 摘要。
2. Agent 先读压缩知识，再按明确引用 drill down。
3. 模型生成内容是有来源、置信和作用域的派生物。
4. 增量 generation 全部验证后才切 current head。
5. 项目必须固定知识 revision/digest，不能实时读取漂移的 Wiki。
6. 工具返回稳定 schema、预算和 gap，禁止 Agent 任意扫描源文件。
7. 自动学习先产生 proposal，经验证后才成为 active knowledge。

### 3.3 不应复制的实现

- 不用 Markdown/JSONL 承担大型媒体事实的主存储。
- 不在每次查询时 `rglob` 全 Wiki。
- 不采用中文单字符 overlap 和固定相关性/时效/重要性权重。
- 不把隐式链接夸大成已实现知识图谱。
- 不采用社交角色领域字段或“重复若干次即生成 Skill”的固定规则。
- 不复制 OriNodes 当前已记录的“实时 Wiki revision 未被 Active Snapshot 固定”缺口。
- 不让 LLM 原地重写旧页面；只生成 revision/delta/supersession。

### 3.4 与 MemoLens 当前代码的准确接缝

本轮不是从空白画架构。当前代码已经有一些应直接保留的“骨头”，也有几处会让 Wiki 放大错误的缺口：

| 当前代码事实 | 已有价值 | 与目标的缺口 | 规范裁决 |
| --- | --- | --- | --- |
| [import_plan.py](../backend/src/media/import_plan.py) 与 [video.py](../backend/src/media/video.py) 已有稳定 asset、source、analysis revision、job stage、segment、keyframe 与 sidecar transcript | 精确 identity、可恢复分析和时间段比“先建 Wiki 页面”更重要 | 图片 legacy path 与新 media path 尚未完全成为同一 canonical truth | 先完成 ML-008 bridge；Wiki 只能读 canonical ledger 并可重建 |
| [video.py](../backend/src/media/video.py) 当前会形成时间 segment 和本地 keyframe，但 summary 主要是文件名、时间和 sidecar 字幕拼接 | 已有可直接复用的粗粒度时间骨架 | 还不是 Action/State/Emotion/Cinematography 等 multi-key 语义理解；不能把 generic summary 宣称为视频理解 | 以现有 segment 为 L0，新增有来源的 Agent observations；query-aware refinement 新建 revision |
| [routes.py](../backend/src/api/routes.py) 同时存在 `/v1/retrieval/query` 与 `/v1/search/mixed`；[mixed_presenter.py](../backend/src/media/mixed_presenter.py) 明确返回 `lexical_local_fallback` / `semantic_available: false` | 当前结果诚实地说明 fallback，而非伪装语义能力 | Photo、mixed、Atlas/CLI 的候选与错误 contract 仍分裂；不能在其上再加第四套 Wiki search | 先由 ML-009 统一 typed intent/evidence，再让 ML-014 编排 search/read/follow |
| [timeline.py](../backend/src/media/timeline.py)、[timeline_operations.py](../backend/src/media/timeline_operations.py)、[timeline_validation.py](../backend/src/media/timeline_validation.py) 和 [render.py](../backend/src/media/render.py) 已有 grounded clip、typed edit、revision/CAS、material validation 与验证后渲染 | 这是可执行、可回退创作链的关键资产，优于重新从 OpenCut fork 一个项目真源 | 当前 Director 仍以规则和逐项候选为主，没有完整 Script Coverage、Craft compilation 与统一 action history | ML-015/016/018 必须编译到现有 typed-domain 思路，而不是输出任意 FFmpeg 或第二种 Timeline |
| [creator_memory.py](../backend/src/media/creator_memory.py) 与 [inbox.py](../backend/src/media/inbox.py) 已有 version/CAS、来源和可撤销状态 | 与“创作者记忆确认后沉淀”“原件不动”一致 | 需要明确它与自动事实记忆、当前项目 Blueprint 的边界，且不能让一次 assignment 静默学习 | 保留实现不变量，用 ML-011/015 做 context 分流和跨项目写入门槛 |
| [memolens_cli.py](../.agents/plugins/plugins/memolens/scripts/memolens_cli.py) 在本调研基线时已提供 status、search、video/media、Creator/Inbox/Memory 与 Timeline draft/revise/validate/read 的结构化入口；ML-014-A0 实施后又增加 Wiki status/list/search/open/evidence | 已证明 Agent 无关 CLI 方向无需另起炉灶；in-memory draft 与只读持久化默认较安全 | 物化 Wiki generation/follow/refine、Creative Blueprint、统一持久写 command、export/package 与跨 Agent resume 仍未交付 | ML-014/015 在同一 Core contract 上逐项扩展；CLI 不得变成 raw SQLite client |
| 现有 Workbench 已有 Storyboard/Timeline/Preview 基础 | 用户要求的可视化表面已有连续演进路径 | 新能力若另建一套 ChatCut UI，会产生双真源和双历史 | 延续当前 MemoLens UI；需要时只选择性复用 OpenCut 的 MIT 能力，不用外部编辑器重置领域内核 |

因此最短且风险最低的演进不是“大重写”：

1. 先把现有 asset/source/analysis/timeline/revision 变成全 surface 唯一真源。
2. 在其上 shadow 生成 ML-014 Wiki projection；验证失败直接丢弃 projection，不伤 facts。
3. 把当前 CLI 从“读与内存 draft”扩展为经 Core 验证的 Agent protocol，不开放数据库或 shell。
4. ML-018 先消费现有 mixed candidate 与 Timeline contract 做可对照的 Coverage Plan，不等待图数据库。
5. 首个可靠 export 同时落 ML-017 usage manifest；这会立刻改善下一次检索，比先扩充高级图关系更直接。

## 4. 活跃开源项目比较

### 4.1 Wiki、Agent memory 与图检索

| 项目 | 2026-08-22 快照 | 已实现机制 | MemoLens 采用判断 |
| --- | --- | --- | --- |
| [Google OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf) | ≈8.8k★；Apache-2.0 | 分层 Markdown bundle、index、来源/验证/新鲜度/生命周期 | 采用为可移植 Wiki profile，不作为内部 DB |
| [LangChain OpenWiki](https://github.com/langchain-ai/openwiki) | ≈15.5k★；MIT | Agent 持续维护 OKF Wiki、写入校验、目录同步、内容 snapshot、CLI | 参考 producer/validator/snapshot 分层；不引入其通用 Agent runtime |
| [DeepWiki Open](https://github.com/AsyncFuncAI/deepwiki-open) | ≈17.7k★；MIT | 代码仓库生成分层 Wiki、Mermaid 和问答，底层仍以文本 RAG 为主 | 只参考生成流程和浏览 UX |
| [PageIndex](https://github.com/VectifyAI/PageIndex) | ≈35.3k★；MIT | 让 LLM 在文档目录树上先定位再钻取，强调 vectorless reasoning | 借“目录先行”与可解释 traversal；文本/PDF 假设不能直接承担视频 |
| [Microsoft GraphRAG](https://github.com/microsoft/graphrag) | ≈35.6k★；MIT | 实体关系、社区层级/报告、Local/Global/DRIFT search | 可作全库主题地图实验；索引成本和摘要损失不适合精确片段主路径 |
| [LightRAG](https://github.com/HKUDS/LightRAG) | ≈39.1k★；MIT | 图+向量双层检索、增量更新、选择性删除 | 作为图检索 baseline；不复制多存储一致性复杂度 |
| [HippoRAG](https://github.com/OSU-NLP-Group/HippoRAG) | ≈4.0k★；MIT | OpenIE 图、查询种子、Personalized PageRank、原文 rerank | 适合跨人物/地点/物件/项目联想实验；文本 OpenIE 不能定义媒体事实 |
| [Graphiti](https://github.com/getzep/graphiti) | ≈30.2k★；Apache-2.0 | episode 来源、双时态事实、失效而非覆盖、BM25+vector+graph | 强参考时态、来源、supersession；视频必须经时间证据层接入 |
| [Letta](https://github.com/letta-ai/letta) | ≈24.3k★；Apache-2.0 | 分层记忆、持久 Agent、Git-backed memory filesystem | 借记忆层次与版本思想，不引入整个 Agent 平台 |
| [Mem0](https://github.com/mem0ai/mem0) | ≈63.8k★；Apache-2.0 | 对话事实抽取、作用域、历史、向量召回 | 适合确认后的 Creator Memory，不适合素材 Wiki 主检索 |
| [Cognee](https://github.com/topoteretes/cognee) | ≈30.2k★；Apache-2.0 | 可组合 add/cognify/memify/search，关系/图/向量适配和 MCP | 参考知识流水线与 adapter 边界；整体引入过重 |
| [LlamaIndex](https://github.com/run-llama/llama_index) | ≈51.8k★；MIT | Router、recursive retrieval、property graph、组合 retriever | 参考接口和路由，不让通用框架成为领域模型 |
| [KAG](https://github.com/OpenSPG/KAG) | ≈9.0k★；Apache-2.0 | Schema 约束图、图与原文互索引、逻辑规划 | 借鉴 schema-first，避免开放抽取生成巨图 |
| [RAG-Anything](https://github.com/HKUDS/RAG-Anything) | ≈23.0k★；MIT | 文本、图片、表格等跨模态双图 | 参考多模态对象表示；它不解决连续视频边界 |
| [RAGFlow](https://github.com/infiniflow/ragflow) | ≈89.0k★；Apache-2.0 | 大规模 parsing、任务、索引和可观测性 | 参考工程恢复和任务 UI，不是知识模型创新源 |

Star 数不能代替代码审计。尤其要区分项目宣传、开源版本和托管版能力；模型权重、数据、框架代码也可能使用不同许可证。

### 4.2 媒体管理项目

| 项目 | 2026-08-22 快照 | 可吸收部分 | 不应照搬 |
| --- | --- | --- | --- |
| [Immich](https://github.com/immich-app/immich) | ≈112.3k★；AGPL-3.0 | durable jobs、OpenAPI contract、模型/索引重跑、媒体 pipeline | 服务端/Redis/Postgres 拓扑和 AGPL 代码 |
| [PhotoPrism](https://github.com/photoprism/photoprism) | ≈40.1k★；仓库许可证需单独审查 | handler 只作 glue、媒体/vision/worker/entity 边界、metadata 来源优先级 | 传统标签库不足以支持创作时间线 |
| [Ente](https://github.com/ente-io/ente) | ≈28.5k★；AGPL-3.0 | 敏感派生数据、端侧 ML、跨 runtime 验证和模型版本 | 云同步/加密拓扑与 AGPL 代码 |
| [Nextcloud Memories](https://github.com/pulsejet/memories) | ≈3.8k★；AGPL-3.0 | 保留用户文件结构，索引、时间线、地图和转码围绕原权限 | Nextcloud 权限/部署耦合 |

这些项目证明“原文件结构是现实、索引是派生能力、长任务必须可恢复”；它们没有解决 MemoLens 的脚本覆盖、使用区间和 Agent 主动复核。

### 4.3 视频与多模态开源

| 项目 / 论文实现 | 状态 | 关键机制 | MemoLens 判断 |
| --- | --- | --- | --- |
| [UniversalRAG, ACL 2026](https://aclanthology.org/2026.acl-long.177/) | 正式论文；[代码](https://github.com/wgcyeo/UniversalRAG) Apache-2.0 | 按模态和粒度把问题路由到文本、图片、短片或完整视频等语料 | 强参考：不要把所有模态塞进同一检索空间 |
| [WorldMM, CVPR 2026 Highlight](https://openaccess.thecvf.com/content/CVPR2026/papers/Yeo_WorldMM_Dynamic_Multimodal_Memory_Agent_for_Long_Video_Reasoning_CVPR_2026_paper.pdf) | 正式论文；[代码](https://github.com/wgcyeo/WorldMM) Apache-2.0 | 多时间尺度 episodic、长期 semantic graph、visual segment memory，Agent 选择记忆 | 与素材 Wiki 高度相关，适合作为层级记忆实验基线 |
| [VideoRAG, KDD 2026](https://github.com/HKUDS/VideoRAG) | 公开实现 | 文本知识图 + 层次多模态上下文 + 自适应检索 | 架构相关；当前 ImageBind 依赖带来非商业限制，不能整库复制 |
| [Video-RAG, NeurIPS 2025](https://github.com/Leon1207/Video-RAG-master) | 正式论文代码，仓库许可证不明确 | OCR/ASR/对象检测与画面时间对齐，再按需补给 LVLM | 借时间对齐证据；不复制无明确许可代码 |
| [VLM2Vec, ICLR 2025](https://github.com/TIGER-AI-Lab/VLM2Vec) | Apache-2.0 | 图片、视频、视觉文档 unified embedding 与时刻检索 | 仅作为候选召回/对照；权重另审 |
| [InternVideo](https://github.com/OpenGVLab/InternVideo) | Apache-2.0 | 视频—文本匹配、运动与时序表示、中英文模型 | 可选检索实验，不与 Core 强耦合 |
| [NVIDIA Video to Data](https://github.com/nvidia-isaac/video_to_data) | Apache-2.0 代码；CC-BY-4.0 文档/Skills | 动作时间段、实体关系、逐帧 embedding、文件化阶段产物、Agent 验证 | 新但工程形态接近；参考强类型阶段与可恢复中间产物 |
| [MM-Mem, ACL 2026](https://github.com/EliSpectre/MM-Mem) | 正式论文；部分代码、无明确许可证 | L1 perception、L2 episodic、L3 symbolic，ADD/MERGE/DISCARD | 研究 spike，不复制代码 |

## 5. 学术路线与可转化结论

### 5.1 长期记忆与层级组织

| 工作 | 发表状态 | 原贡献 | MemoLens 转化 |
| --- | --- | --- | --- |
| [RAPTOR](https://proceedings.iclr.cc/paper_files/paper/2024/hash/8a2acd174940dbca361a6398a4f9df91-Abstract-Conference.html) | ICLR 2024 | 递归聚类/摘要形成树，从不同抽象层检索 | Library → event → asset → shot → moment 的导航候选；摘要不能替代 evidence |
| [A-MEM](https://papers.neurips.cc/paper_files/paper/2025/file/19909c36f51abc4856b4560aff3d36d6-Paper-Conference.pdf) | NeurIPS 2025 | Zettelkasten 式原子 note、动态 link 与演化记忆 | Wiki 页与关系可演进，但需 revision、source 与用户确认等级 |
| [HippoRAG 2](https://proceedings.mlr.press/v267/gutierrez25a.html) | ICML 2025 | 非参数 continual memory、图式关联和多跳 retrieval | 关系扩展作为 baseline；必须回到媒体 span |
| [MELODI](https://deepmind.google/research/publications/121073/) | ICLR 2025 | 短期/长期层级压缩，较强基线降低 8× memory footprint | 启发多频率压缩；不在 App 内复制模型训练架构 |
| [Memory Consolidation](https://proceedings.mlr.press/v235/balazevic24a.html) | ICML 2024 | 用过去激活的非参数压缩延伸视频上下文 | 支持多尺度视频记忆；不是现成 DB 设计 |
| [Titans](https://proceedings.neurips.cc/paper_files/paper/2025/hash/a4ca07aa108036f80cbb5b82285fd4b1-Abstract-Conference.html) | NeurIPS 2025 | surprise 驱动的测试时神经长期记忆 | “独特/新奇/与库中差异”可做素材排序信号；不训练 Titans |

### 5.2 Agentic retrieval 与证据充分性

| 工作 | 发表状态 | 可转化机制 |
| --- | --- | --- |
| [Adaptive-RAG](https://aclanthology.org/2024.naacl-long.389/) | NAACL 2024 | 按问题复杂度路由不检索、单步或多步检索 |
| [DRAGIN](https://aclanthology.org/2024.acl-long.702/) | ACL 2024 | 在推理过程中决定何时检索和检索什么 |
| [Self-RAG](https://openreview.net/pdf?id=hSyW5go0v8) | ICLR 2024 | 反思检索必要性、相关性、支持度和完整性 |
| [Sufficient Context](https://openreview.net/pdf?id=Jjr2Odj8DJ) | ICLR 2025 | 区分 evidence 不足与模型拿到充分 evidence 仍推理错误 |
| [ReadAgent](https://deepmind.google/research/publications/74917/) | Google/DeepMind 2024 预印本 | 先读 gist memory，需要细节时主动查回原文，实验有效上下文扩大 3–20× |
| [LLM-Wiki](https://arxiv.org/abs/2605.25480) | 2026 预印本 | 文档编译成双向链接 Wiki；Agent search/read/follow；Error Book 跨批次纠错 |
| [DocNavRAG](https://arxiv.org/abs/2608.01565) | 2026-08 预印本 | locate/navigate/expand/fetch 与证据充分性状态 |

后两项和 WikiLoop 等 2026 工作尚未经过足够独立复现。本项目可把它们变成有 kill criteria 的实验，不能宣传成行业定论。

### 5.3 长视频主动感知

| 工作 | 发表状态 | 直接启示 |
| --- | --- | --- |
| [TimeChat](https://openaccess.thecvf.com/content/CVPR2024/html/Ren_TimeChat_A_Time-sensitive_Multimodal_Large_Language_Model_for_Long_Video_CVPR_2024_paper.html) | CVPR 2024 | 时间敏感理解必须显式定位 timestamp |
| [VideoAgent](https://eccv.ecva.net/virtual/2024/poster/1090) | ECCV 2024 | Agent 迭代选择需要看的帧，而非看完整视频 |
| [VideoTree](https://openaccess.thecvf.com/content/CVPR2025/html/Wang_VideoTree_Adaptive_Tree-based_Video_Representation_for_LLM_Reasoning_on_Long_CVPR_2025_paper.html) | CVPR 2025 | 按查询动态建立粗到细视频树 |
| [ReWind](https://openaccess.thecvf.com/content/CVPR2025/html/Diko_ReWind_Understanding_Long_Videos_with_Instructed_Learnable_Memory_CVPR_2025_paper.html) | CVPR 2025 | 按指令保留相关动态记忆，再读取高分辨率候选 |
| [LongVU](https://proceedings.mlr.press/v267/shen25j.html) | ICML 2025 | 视觉相似、文本引导和空间压缩降低长视频 token |
| [Video Panels](https://openaccess.thecvf.com/content/CVPR2026/html/Doorenbos_Video_Panels_for_Long_Video_Understanding_CVPR_2026_paper.html) | CVPR 2026 | 多帧拼图用空间换时间覆盖，适合 Agent 初筛 |
| [VideoARM](https://openaccess.thecvf.com/content/CVPR2026/html/Yin_VideoARM_Agentic_Reasoning_over_Hierarchical_Memory_for_Long-Form_Video_Understanding_CVPR_2026_paper.html) | CVPR 2026 | Observe–Think–Act–Memorize + 层级记忆和工具调用 |
| [Active Video Perception](https://openaccess.thecvf.com/content/CVPR2026F/html/Wang_Active_Video_Perception_Iterative_Evidence_Seeking_for_Agentic_Long_Video_CVPRF_2026_paper.html) | CVPR 2026 Findings | Plan–Observe–Reflect，主动寻找时间证据与充分性判断 |
| [Graph-to-Frame RAG](https://openaccess.thecvf.com/content/CVPR2026/html/Yang_Graph-to-Frame_RAG_Visual-Space_Knowledge_Fusion_for_Training-Free_and_Auditable_Video_CVPR_2026_paper.html) | CVPR 2026 | 相关子图渲染为可审计视觉图，再交给多模态模型 |
| [WorldMM](https://openaccess.thecvf.com/content/CVPR2026/html/Yeo_WorldMM_Dynamic_Multimodal_Memory_Agent_for_Long_Video_Reasoning_CVPR_2026_paper.html) | CVPR 2026 Highlight | episodic / semantic / visual 三类动态记忆由 Agent 选择 |

共同结论不是“某个模型最好”，而是：低成本粗索引 → 查询相关区域 → 粗到细导航 → 少量原始多模态复核 → 证据充分性判断。

### 5.4 多模态和粒度路由

- [UniversalRAG, ACL 2026](https://aclanthology.org/2026.acl-long.177/)：根据查询路由到不同模态和粒度的 corpus。
- [M3KG-RAG, CVPR 2026](https://openaccess.thecvf.com/content/CVPR2026/html/Park_M3KG-RAG_Multi-hop_Multimodal_Knowledge_Graph-enhanced_Retrieval-Augmented_Generation_CVPR_2026_paper.html)：多模态分别检索，再基于问题做有依据裁剪。
- [VisRAG, ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/hash/3640a1997a4c9571cea9db2c82e1fc35-Abstract-Conference.html)：视觉材料全部转文字会丢信息。
- [Gemini Embedding 2](https://developers.googleblog.com/en/building-with-gemini-embedding-2/)：统一跨模态空间适合候选召回，但当前视频时长/帧采样/音轨边界说明它不能替代独立时序与音频证据。

**MemoLens 裁决**：统一 contract，不统一所有表示空间。Action、Dialogue、Object/State、Scene/Place、Emotion、Cinematography、Narrative Function、Technical Quality 和 Summary 应保留可区分 multi-key 信号。

## 6. 建议架构：Creator Media Knowledge Fabric

```text
┌───────────────────────────────────────────────────────────────┐
│  Human + Agent                                                │
│  intent / stance / reference / feedback / final decision      │
└───────────────────────────┬───────────────────────────────────┘
                            │
┌───────────────────────────▼───────────────────────────────────┐
│  Creative Plane                                               │
│  Creative Blueprint → Script Coverage Graph → Timeline        │
│  → Operation History → Export / Usage Manifest                │
└───────────────────────────┬───────────────────────────────────┘
                            │ evidence bundles
┌───────────────────────────▼───────────────────────────────────┐
│  Agent Retrieval Plane                                        │
│  plan → route → search/browse → follow → inspect → sufficient │
│  → query-aware refinement → trace                             │
└───────────────────────────┬───────────────────────────────────┘
                            │
┌───────────────────────────▼───────────────────────────────────┐
│  Agent Wiki / Navigation Plane                                │
│  Library / Event / Theme / Asset / Project / Work pages       │
│  OKF-compatible readable snapshot + typed relation views      │
└───────────────────────────┬───────────────────────────────────┘
                            │ rebuildable projections
┌───────────────────────────▼───────────────────────────────────┐
│  Temporal Multimodal Knowledge Plane                          │
│  episodic multi-key memory + typed temporal relations         │
│  FTS / semantic / temporal / usage / graph projections        │
└───────────────────────────┬───────────────────────────────────┘
                            │
┌───────────────────────────▼───────────────────────────────────┐
│  Canonical Evidence Ledger                                    │
│  Asset / Source / Span / Observation / Analysis Revision      │
│  User Confirmation / Payload Manifest / Usage Interval        │
└───────────────────────────┬───────────────────────────────────┘
                            │
┌───────────────────────────▼───────────────────────────────────┐
│  Original Library                                             │
│  one local folder; originals are not moved or overwritten     │
└───────────────────────────────────────────────────────────────┘
```

### 6.1 Canonical Evidence Ledger

至少保存：

- `asset_id`、原件 hash、source observation、媒体技术参数；
- shot / micro-segment / keyframe / panel 的精确时间边界；
- transcript、OCR、静音/气口、声音事件和可用的对齐信息；
- 每个 observation 使用的模型/Agent、prompt/schema、输入 hash、时间和置信/未知；
- 用户修正与 authority；
- 导出作品、timeline revision 与原素材 usage interval；
- 发给外部 Agent 的 payload manifest。

任何 Wiki、图、embedding 或摘要都不能反向覆盖它。

### 6.2 Episodic multi-key media memory

一个视频 span 不能只有一个总摘要。候选 key 包括：

- Action / Event：发生了什么；
- Dialogue / Mention：说了什么；
- Object / State：出现什么、状态如何变化；
- Scene / Place / People：在哪里、谁在场；
- Emotion / Atmosphere：情绪与氛围；
- Cinematography：景别、构图、运镜、光线和色彩；
- Narrative Function：hook、establishing、reaction、B-roll、turn、payoff；
- Technical Quality：清晰、稳定、噪声、可裁切空间；
- Compact Summary：给人和 Agent 快速浏览。

每个 key 可以由不同证据和模型产生，不能强迫进入一个 embedding space。

### 6.3 有限、类型化的时序关系

节点可以包含：

```text
Asset, Scene, Shot, Moment, FramePanel, SpeechSpan, SoundEvent,
Event, Episode, PersonCandidate, Place, Object, Action, Mood, Theme,
StoryFunction, Project, BlueprintBeat, ReferenceWork, TechniqueCard,
TimelineClip, Export, UsageInterval, CreatorPreferenceRevision
```

边可以包含：

```text
contains, before, after, overlaps, member_of, depicts, spoken_in,
supports, contradicts, similar_to, same_event_hypothesis,
matches_blueprint, selected_in, used_in, inspired_by,
derived_from, supersedes, invalidates
```

每条语义边至少携带 evidence、confidence/unknown、observed time、valid time、analysis/model revision 和 authority。首阶段先用类型化关系 projection；独立图数据库只有在 benchmark 上有实质收益才晋级。

### 6.4 Wiki 是地图，Evidence 是领土

自动/按需形成：

- Library coverage/index 页面；
- event/episode、place、theme 和人物候选页面；
- asset 页面与 span evidence view；
- project、work、export 和素材使用页面；
- Creator Memory 的确认 revision 概览；
- Reference 与 Technique Card 页面。

页面生成必须有 source、generated、verified、status、stale、revision 和 digest。微小时段不默认一段一个 Markdown 文件；Agent 可通过 evidence tool 动态展开。

### 6.5 Agent 检索协议

需要原子能力，而不是只有一个 `search(query)`：

```text
wiki.list / wiki.open
search.plan / search.find
relation.expand / temporal.neighbors
evidence.read / moment.inspect
analysis.request / analysis.attach
search.sufficient / search.trace
feedback.record
```

典型路由：

- 精确台词 → transcript/OCR/FTS；
- 动作或画面 → visual/action multi-key；
- 某次旅行 → Wiki event + metadata；
- 关联人物/地点/物件 → typed relation/PPR 实验；
- 文稿配画面 → 语义、叙事功能、情绪、时长、质量和 usage 联合；
- 动作前后因果 → 命中 span 后展开 temporal neighbors；
- 全库主题 → 层级 Wiki/社区摘要；
- 低置信 → panel → zoom → short clip 的第二阶段复核。

每条路径都必须限制 hop、节点、字符/token、帧、短片时长、远端调用和隐私预算。

### 6.6 Script Coverage Graph：MemoLens 自己最值得做的部分

逐句独立 top-k 不是剪辑。应该把文稿全局分配建模为：

```text
Blueprint beats / sentences / emotional turns / narrative functions
                          ↕
candidate image or video spans
```

边表达：语义、事实支持、氛围、叙事功能、动作连续性、视线方向、情绪曲线、风格、时长、画质、画幅可裁切性和历史使用。

全局选择需要同时避免：

- 同一镜头被多个段落重复占用；
- 每段单独匹配但整体风格/节奏断裂；
- 只匹配关键词，不形成起承转合；
- 已用片段反复出现，而同文件剩余画面被浪费。

这是外部 RAG/相册产品没有替 MemoLens 解决的领域核心。

### 6.7 反馈驱动但不静默学习

记录：

```text
query → candidates → preview → accept/reject/replace
→ timeline operation → export revision → later reuse
```

写入分流：

- 本项目的一次选择 → 只进入 Blueprint/operation history；
- 多项目稳定模式 → 形成 Creator preference suggestion，确认后生效；
- 素材事实纠正 → 用户确认 evidence assertion；
- 搜索/编译失败 → compiler issue/Error Book，提出 Wiki 修复；
- 导出 → 自动写 usage fact。

Wiki Builder 的改动必须由后续检索指标和用户结果验证，不能因为模型说“摘要更好”就晋级。

## 7. 它为什么不是传统 RAG

| 维度 | 传统 flat RAG | MemoLens 目标 |
| --- | --- | --- |
| 基本单元 | 文本 chunk | asset + 精确多模态时间 span |
| 组织 | 扁平向量集合 | 层级 Wiki + typed temporal relations + high-fidelity evidence |
| 检索 | 一次 top-k | plan → route → search/read/follow/inspect → sufficient |
| 语义 | 一个 embedding 承担相似度 | transcript、visual、action、audio、technical、usage 等多 key late fusion |
| 更新 | 重切块/重向量 | 不可变 observation + revision + supersession + generation |
| 可信度 | chunk 来源或无来源 | claim-level evidence、authority、freshness、payload、analysis version |
| 返回 | 用于回答的文字 | 可直接进 Timeline 的素材 Evidence Bundle |
| 使用历史 | 通常不存在 | 精确 usage interval、residual interval、项目/导出 provenance |
| Agent 行为 | 读给定上下文 | 主动决定下一页、下一条边和是否放大原素材 |
| 创作结果 | 生成答案 | 全局镜头分配、可编辑 Timeline、操作历史与素材包 |

## 8. 可声称与不可声称的创新

### 可以作为“创新假设”推进

1. **Usage-aware temporal memory**：素材不是已用/未用二值，而是精确区间和剩余价值。
2. **Script Coverage Graph**：把整篇文稿的叙事覆盖作为全局匹配问题，不做逐句独立 top-k。
3. **Wiki → evidence → timeline 的双向闭环**：检索产物可执行，导出结果又反哺素材事实。
4. **Craft knowledge compiler**：把电影知识与参考样片拆为有条件的技巧卡，再根据 Blueprint 和可用素材编译 typed operations。
5. **Agent-hosted semantics**：MemoLens 不自建第二个聊天/模型账户；任何 Agent 经同一 CLI/Core 获取有界媒体证据并写回版本化 observation。
6. **Human learning loop**：系统解释专业选择，人负责思想与取舍，AI 负责繁琐实现。

这些都是待验证的系统创新，不能在 benchmark 前宣传为质量结果。

### 不能当作创新

- 使用向量数据库、知识图谱或大模型；
- 把视频每隔 N 秒抽帧并生成摘要；
- 自动生成 Markdown Wiki；
- 接入 Codex/Claude；
- 用 OpenCut/OpenChatCut 做编辑器；
- 页面或节点很多；
- 没有用户结果指标的“自演化”。

## 9. 建议的预注册实验

### E1：Flat retrieval vs Agent Wiki traversal

- 数据：至少 200 个复杂创作查询，包含语义、时间、事件、usage、无答案和多跳。
- 公平性：固定底层 analysis、Agent/model、token/帧/时间预算。
- 指标：5 分钟内加入正确 Timeline span 的成功率、Recall@K、tIoU、工具调用、延迟、证据正确率。
- 晋级：相对最强 baseline 成功率 +10 个百分点，简单查询非劣。

### E2：固定抽帧 vs Panel → Zoom

- 比较：固定均匀帧、镜头代表帧、panel 初筛 + query-aware zoom。
- 指标：visual token、调用量、`R@5@IoU≥0.5`、边界误差。
- 晋级：成本降低 ≥60% 且 Recall 下降 ≤2 个百分点，或同预算 tIoU 提升 ≥10 个百分点。

### E3：单摘要 vs episodic multi-key

- 分桶：台词、动作、物体状态、情绪、电影语言、技术质量、画幅可裁切性。
- 指标：Recall@10、用户盲选偏好、错误解释。
- 晋级：总体 Recall@10 +10 个百分点，且无单类回归超过 3 个百分点。

### E4：逐句 top-k vs Script Coverage Graph

- 任务：8–20 个 script beat 的完整短视频。
- 指标：首轮接受率、重复镜头、叙事覆盖、替换操作数、主动编辑时间。
- 晋级：完整初剪盲选胜率 +20%，主动操作数 -40%，硬约束违反为 0。

### E5：有/无 usage interval

- 任务：找未使用、找同文件剩余、允许复用、排除成片。
- 指标：区间判断准确率、误排除、重复使用率、剩余素材发现率。
- 晋级：已用区间规避准确率 ≥95%，同时不把未用 remainder 错误排除。

### E6：版本化 Wiki rebuild

- 操作：更换 Agent/model/prompt schema、全量重建、单资产增量、用户修正。
- 指标：原件 hash、历史项目 binding、evidence trace、stale page、current generation 一致性。
- 门槛：历史引用破坏为 0，半建 current 为 0，用户确认保留 100%。

## 10. Spec 映射

| 目标 | Spec |
| --- | --- |
| Canonical asset/source/analysis 与 projection | [ML-008](specs/008-unified-media-memory-kernel/spec.md) |
| Typed intent、honest empty、fusion 与 evidence result | [ML-009](specs/009-intent-evidence-retrieval/spec.md) |
| 精确时间 span、event/episode 与 query-guided refinement | [ML-010](specs/010-hierarchical-temporal-memory/spec.md) |
| Creator preference 确认后写入 | [ML-011](specs/011-consentful-creator-model/spec.md) |
| Story claim 与 evidence compiler | [ML-012](specs/012-verifiable-story-compiler/spec.md) |
| Agent Wiki、关系导航、generation 与项目 binding | [ML-014](specs/014-agent-navigable-media-wiki/spec.md) |
| Agent 无关 CLI、Creative Blueprint 和开放项目链 | [ML-015](specs/015-agent-agnostic-creative-protocol/spec.md) |
| Craft Wiki、Technique Card 与专业知识编译 | [ML-016](specs/016-craft-wiki-technique-compiler/spec.md) |
| 导出 usage ledger 与轻量/完整素材包 | [ML-017](specs/017-export-usage-ledger-material-package/spec.md) |
| Script Coverage Graph 与全片级素材分配 | [ML-018](specs/018-script-coverage-global-footage-assignment/spec.md) |

## 11. 采用顺序

1. 先完成 ML-008 的统一事实账本与可恢复分析；否则 Wiki 只会放大现有分裂。
2. 完成 ML-009 typed/honest retrieval，固定无 Wiki baseline。
3. 以现有 segment 为基础做 ML-014 的只读 Wiki projection 和工具导航，不先引入图数据库。
4. 增加 panel → zoom 的 Agent analysis protocol，并保留 payload manifest。
5. 通过 complex retrieval benchmark 后，才让 Wiki traversal 成为默认慢路径。
6. 再做 ML-018 Script Coverage Graph、ML-016 Craft Compiler 和 ML-017 export usage closed loop；其中 ML-017 的基础精确使用记录应随首个可靠 export 一起交付，不等待高级技巧。
7. 只有类型化关系表在真实多跳任务上不足时，才 bake-off 独立图存储。

## 12. 明确不采用

- 视频一旦转成 TXT/JSON 就不再回看原视频。
- 所有模态共享一个向量空间和一个分数。
- 全库每次查询重新分析。
- LLM 开放式抽取无限实体和关系。
- Wiki、图、向量、项目各自成为可写真源。
- 未确认的项目选择自动成为长期创作者偏好。
- 热点网页、电影理论和私人素材事实混进同一无来源知识层。
- 为“下一代”标签提前引入 Neo4j、云服务、模型训练或通用 Agent scheduler。
- 复制许可证不兼容或不明确的项目代码。

## 13. 调研限制

- Star、release、许可证和产品能力是 2026-08-22 快照；实施前必须重新核验。
- 2026 年 LLM-Wiki、WikiLoop、DocNavRAG 等属于前沿预印本，不具备顶会正式论文同等证据等级。
- 公开视频 QA benchmark 与私人自媒体剪辑存在分布差异；外部论文只能提出机制候选，不能替代 MemoLens 自有评测。
- 本调研没有执行外部项目的完整本地 benchmark，也没有授权引入任何依赖。
- OriNodes-v4 的结论来自本地只读代码审计；未修改其文件或历史。

## 14. 本轮边界

本文是研究与架构建议。它不授权修改 MemoLens 产品代码、数据库、CLI、配置、构建、模型或用户媒体。
