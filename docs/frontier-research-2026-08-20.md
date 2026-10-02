# MemoLens 前沿开源与学术调研

- 调研日期：2026-08-20
- 资料范围：优先使用官方文档、官方仓库、论文原文与标准正文
- 结论性质：外部事实与对 MemoLens 的推断分开标记
- 实施授权：无

## 1. 核心结论

私人媒体系统的下一代瓶颈不是“再换一个更强 embedding”。真实查询同时包含画面、人物关系、时间、地点、事件、否定条件和用户意图。一个统一向量很难精确表达这些条件；让通用 Agent 自由调用更多工具也可能因为错误交集而变差。

MemoLens 最有潜力的目标形态是：

> 本地优先、证据驱动、时间感知、可修正、可追溯的个人媒体记忆操作系统。

外部资料支持五个架构方向：

1. 原媒体与派生索引分离，派生数据可删除重建。
2. 图片、视频、元数据和人物关系用专门信号召回，再显式融合。
3. 简单查询走快路径，困难查询才使用多步推理和定向视觉回看。
4. 记忆更新保留有效时间、系统观察时间、来源和被替代关系。
5. 创作先建立带证据的 beat/shot，再生成文案、时间线和导出 provenance。

这些结论是研究转化，不是现成实现。每一项都需要 [Spec 004](specs/004-evidence-backed-retrieval-privacy-benchmark/spec.md) 的项目级 benchmark 证伪。

## 2. 阅读规则

本文使用以下标签：

- **事实**：来源直接陈述或实现可在官方仓库验证。
- **MemoLens 推断**：从外部机制映射到本项目的建议，不是来源作者结论。
- **实验候选**：只有通过预注册比较后才能进入正式架构。
- **风险**：许可证、规模、硬件、成熟度或结论外推边界。

### 2.1 来源证据登记

以下登记把来源状态、作者原结论与 MemoLens 推断分开。日期为正式发表年份、标准版本日期或本轮访问截止；“成熟度”只表示证据/实现状态，不表示适合直接集成。

#### 开源产品与本地检索基础设施

| 来源 | 状态 / 日期 | 原结论或已实现机制 | MemoLens 推断 | 建议实验 | 成熟度 | 风险与外推边界 |
| --- | --- | --- | --- | --- | --- | --- |
| [Immich](https://docs.immich.app/developer/architecture/) | 活跃生产开源项目；访问 2026-08-20 | API、worker、ML 与 persistence 分层，客户端 contract 可生成 | 复用 durable job 和薄 API 语义，不复制服务拓扑 | 进程 kill/resume、contract generation、模型升级 reanalysis | 生产实现 | 官方架构注明目标与代码可能有差异；服务器拓扑不适合照搬桌面 |
| [Ente](https://github.com/ente-io/ente/blob/main/docs/docs/photos/features/search-and-discovery/machine-learning.md) | 活跃生产开源项目；访问 2026-08-20 | 端侧 ML 与加密派生索引，跨 runtime 做性能/一致性工作 | 派生 embedding、人物与偏好也按敏感数据治理 | 不同 CPU/runtime golden parity、删除闭包、模型 manifest | 生产实现 | 云同步/移动端假设不同；模型代码与权重许可需分开审查 |
| [PhotoPrism](https://github.com/photoprism/photoprism/blob/develop/CODEMAP.md) | 活跃生产开源项目；访问 2026-08-20 | Handler 是 glue，index/vision/worker/entity 有包边界和来源优先级 | Route 下沉到 application use case，统一 metadata authority | 用同一 fixture 验证各来源优先级与 projection 重建 | 生产实现 | 传统标签/服务器部署不能直接解决个人叙事记忆 |
| [Nextcloud Memories](https://github.com/pulsejet/memories) | 活跃生产开源项目；访问 2026-08-20 | 保留用户文件结构，索引、时间线、地图与转码建立在原权限上 | 文件是原始事实源，Asset ledger 记录观察与派生 | 删除索引后重建、权限撤销、移动文件与历史引用 | 生产实现 | 依赖 Nextcloud 权限和生态，不能照搬部署边界 |
| [LanceDB](https://docs.lancedb.com/search/hybrid-search) | 活跃开源引擎；访问 2026-08-20 | 提供 embedded vector/FTS/SQL 与 hybrid 能力 | 只作为可重建 projection 候选 | 1k/10k/100k update/delete/recovery/Recall/RSS bake-off | 工程候选 | 版本、恢复和分发成本未在 MemoLens 证明 |
| [Qdrant](https://qdrant.tech/documentation/search/hybrid-queries/) | 活跃开源服务；访问 2026-08-20 | Dense/sparse、多阶段 prefetch、named/multivector 与 filter/fusion | 可作为检索质量和多向量上界 | 与 embedded baseline 做 compute-matched 比较 | 生产实现，对本项目是研究对照 | 独立服务会扩大桌面运维、更新与资源面 |
| [Weaviate](https://docs.weaviate.io/weaviate/search) | 活跃开源服务；访问 2026-08-20 | BM25F、vector、hybrid、named vectors 与 filter | 用于核对 hybrid contract，而非预先选型 | 相同 corpus 比较 quality/latency/restore | 生产实现，对本项目是研究对照 | 服务复杂度可能没有质量收益 |

#### 私人检索、长期记忆与自适应回忆

| 来源 | 状态 / 日期 | 原论文结论 | MemoLens 推断 | 建议实验 | 成熟度 | 风险与外推边界 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [PhotoBench](https://doi.org/10.1145/3770855.3817511) / [arXiv](https://arxiv.org/abs/2603.01493) | KDD 2026 Datasets & Benchmarks 正式清单与 DOI；arXiv 为预印本版本 | 私人相册查询存在 modality gap 与 source fusion paradox，并包含 Zero-GT | 类型化视觉/时空/社交/事件 clause，独立召回并审计融合 | validation 上做 paired source ablation、拒答与 protected planner track | 同行评审 benchmark | 仅三个相册；论文 1,188 queries，当前仓库 300+887=1,187，以冻结 release manifest 为准；CC BY-NC 4.0 |
| [LongMemEval](https://proceedings.iclr.cc/paper_files/paper/2025/file/d813d324dbf0598bbdc9c8e79740ed01-Paper-Conference.pdf) | ICLR 2025 正式论文 | 长对话记忆需测信息提取、多会话/时间推理、知识更新与拒答 | Creator/Event memory 不只测一次 Recall | 修正、过期事实、跨会话与拒答案例 | 同行评审 benchmark | 文本对话不是媒体证据，不能直接外推视觉/时间定位 |
| [MemoryAgentBench](https://proceedings.iclr.cc/paper_files/paper/2026/file/fd1eff9dd295df50a41f2521942fa31d-Paper-Conference.pdf) | ICLR 2026 正式论文 | 将 Agent memory 拆为检索、学习、长期更新等能力 | 把学习、查询和遗忘分别验收 | Event/Creator 的测试时学习与 selective forgetting track | 同行评审 benchmark | Agent 对话任务与个人相册分布不同 |
| [BEAM](https://proceedings.iclr.cc/paper_files/paper/2026/file/d7f0cfa0fe759b033d5262e1bb7d4065-Paper-Conference.pdf) | ICLR 2026 正式论文 | 强调可演化长期记忆与更细的能力评估 | 记忆更新需保留 supersede/invalidates 和时间 | 双时间 assertion 与冲突修正集 | 同行评审 benchmark | 任务和存储模型不能替代 MemoLens 领域评测 |
| [Zep temporal graph paper](https://arxiv.org/abs/2501.13956) / [Graphiti](https://github.com/getzep/graphiti) | 厂商关联 arXiv 预印本，2025；活跃开源实现 | 使用事件时间与 ingest/观察时间维护可更新关系 | Event/person/preference 采用双时间 assertion | 先与类型化关系表比较更新、多跳与删除 | 预印本 + 工程实现 | 厂商评测与产品有关联；全图复杂度可能不值 |
| [A-MEM](https://papers.neurips.cc/paper_files/paper/2025/file/19909c36f51abc4856b4560aff3d36d6-Paper-Conference.pdf) | NeurIPS 2025 正式论文 | 结构化 note、链接与演化记忆可改善 Agent memory | 高层 memory 可演化，但原始 observation 不改写 | Note/link 与简单 relation baseline 消融 | 同行评审研究 | 文本 Agent 结论不等于私人媒体图 |
| [HippoRAG 2](https://proceedings.mlr.press/v267/gutierrez25a.html) / [arXiv](https://arxiv.org/abs/2502.14802) | ICML 2025 / PMLR 267 正式论文；arXiv 为版本补充 | 图式关联支持长期、多跳 retrieval | 只有多跳基准证明收益才启用图层 | 多跳/冲突集与 relational baseline 比较 | 同行评审研究 | 图构建和运维成本高，公开语料与个人库不同 |
| [RF-Mem](https://proceedings.iclr.cc/paper_files/paper/2026/file/58f2612e86e2671432499017d049fcb0-Paper-Conference.pdf) | ICLR 2026 正式论文 | 用 familiarity 决定直接回答或昂贵 recollection | 搜索采用快/慢双路径，触发信号需校准 | simple/hard query 的 routing、Recall、cost 与错误路由 | 同行评审研究 | 原任务是长期对话记忆，不是媒体检索 |
| [WorldMM](https://openaccess.thecvf.com/content/CVPR2026/html/Yeo_WorldMM_Dynamic_Multimodal_Memory_Agent_for_Long_Video_Reasoning_CVPR_2026_paper.html) | CVPR 2026 正式论文 | 动态多尺度 episodic/semantic/visual memory 支持长视频推理 | 分层视频 memory 保留细节回链 | 固定采样 vs 分层 memory，测 R@1/tIoU/token | 同行评审研究 | 大模型/长视频环境可能超桌面预算 |
| [Interactive Episodic Memory / ReFocus](https://openaccess.thecvf.com/content/CVPR2026/papers/Subedi_Interactive_Episodic_Memory_with_User_Feedback_CVPR_2026_paper.pdf) | CVPR 2026 正式论文 | 用户反馈可改进 episodic temporal localization，朴素使用也可能退化 | Media Inbox 应先 shadow，并与随机反馈比较 | 相同 30 次预算的 active/random/no-learning | 同行评审研究 | 视频定位反馈不等于长期 Creator preference |

#### 多模态、时间证据、隐私与 provenance

| 来源 | 状态 / 日期 | 原论文或标准结论 | MemoLens 推断 | 建议实验 | 成熟度 | 风险与外推边界 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [MM-Embed](https://proceedings.iclr.cc/paper_files/paper/2025/hash/6d5d6afa9957cfc9142ba60e78a467e9-Abstract-Conference.html) | ICLR 2025 正式论文 | 系统评估 multimodal embedding，规模不自动保证跨任务最佳 | 小模型、语言和任务分开评测 | 中文/OCR/人物关系/近重复 Recall 与 latency | 同行评审研究 | 公开任务不代表私人相册 |
| [UniIR](https://www.ecva.net/papers/eccv_2024/papers_ECCV/html/11927_ECCV_2024_paper.php) | ECCV 2024 正式论文 | 统一 multimodal information retrieval 的数据与模型评估 | 共享 contract 不等于共享所有 embedding space | 与专用视觉/text/metadata late fusion 对比 | 同行评审研究 | 通用 benchmark 缺私人时空/社交约束 |
| [MobileCLIP2](https://machinelearning.apple.com/research/mobileclip2) | TMLR 2025 Featured；Apple 官方研究页 | 强调端侧图文表示的效率/质量权衡 | 作为本地 visual semantic 候选 | Apple silicon CPU/GPU latency、memory、Recall | 同行评审研究 + 官方实现 | 端侧效率仍需看许可、权重与 Intel 路径 |
| [SigLIP 2](https://arxiv.org/abs/2502.14786) | arXiv 预印本，2025 | 改进多语言、定位与图文表示 | 候选 visual semantic space，不承担精确 metadata | 中英、定位和低光分桶 | 研究预印本 | 模型体积、许可与桌面分发未验证 |
| [InternVideo2](https://eccv.ecva.net/virtual/2024/poster/1476) / [arXiv](https://arxiv.org/abs/2403.15377) | ECCV 2024 正式论文；arXiv/开源模型为版本补充 | 扩展视频 foundation representation | 只作为高成本研究上界或 rerank | 少量 candidate window 的质量/成本 | 同行评审研究 + 开源模型 | 资源和依赖不适合默认桌面路径 |
| [LanguageBind](https://proceedings.iclr.cc/paper_files/paper/2024/hash/2862ccf01e3843c81623b246895bcc45-Abstract-Conference.html) / [arXiv](https://arxiv.org/abs/2310.01852) | ICLR 2024 正式论文；arXiv 为版本补充 | 将 image/video/audio/depth 等模态对齐到语言 | 可测试跨模态召回，但 space/version 必须独立 | audio/video/text 共享与专用 space 消融 | 同行评审研究 | 对齐空间可能损失专用精度，audio 尚非当前 P0 产品能力 |
| [Matryoshka Representation Learning](https://arxiv.org/abs/2205.13147) | NeurIPS 2022 正式工作，链接为 arXiv | 一个表示可在不同截断维度下使用 | 可测试 storage/Recall 的维度预算曲线 | 维度、dtype、ANN、Recall、disk 消融 | 同行评审研究 | 不是任意模型都具备该性质 |
| [TimeChat](https://openaccess.thecvf.com/content/CVPR2024/html/Ren_TimeChat_A_Time-sensitive_Multimodal_Large_Language_Model_for_Long_Video_CVPR_2024_paper.html) | CVPR 2024 正式论文 | 时间敏感长视频理解需要显式 timestamp 与定位 | Video evidence identity 必须包含 `[start,end]` | temporal grounding 与边界误差 | 同行评审研究 | 大模型回答不能替代来源时间段 |
| [LongVideoBench](https://proceedings.neurips.cc/paper_files/paper/2024/hash/329ad516cf7a6ac306f29882e9c77558-Abstract-Datasets_and_Benchmarks_Track.html) | NeurIPS 2024 Datasets & Benchmarks | 长视频理解需独立评估时间定位和长上下文 | 建立固定长视频 stress track | 片段长度、token 压缩与准确率曲线 | 同行评审 benchmark | 公开视频与生活媒体分布不同 |
| [LongVU](https://proceedings.mlr.press/v267/shen25j.html) | ICML 2025 正式论文 | 通过视觉/文本引导与空间压缩减少长视频 token | Query-guided refinement 只回看受限窗口 | 与 1fps/compute-matched adaptive baseline 比较 | 同行评审研究 | 压缩可能漏掉短关键事件 |
| [EgoLife](https://openaccess.thecvf.com/content/CVPR2025/html/Yang_EgoLife_Towards_Egocentric_Life_Assistant_CVPR_2025_paper.html) | CVPR 2025 正式论文 | 小时/天级生活记录需要长期、多模态记忆 | Episode 层必须保留时间和原始片段反向边 | 长周期事件/更新/拒答集 | 同行评审研究 | 第一视角与普通相册/家庭视频不同 |
| [Ego4D Episodic Memory](https://ego4d-data.org/docs/benchmarks/episodic-memory/) | 官方 benchmark；访问 2026-08-20 | 使用 Recall@k 与 temporal IoU 评估 episodic localization | MemoLens 视频评测同时看命中与边界 | `R@1@IoU`、mAP、mean tIoU、boundary error | 成熟 benchmark | Ego4D 场景不是私人相册，许可/隐私另审 |
| [EgoPrivacy](https://proceedings.mlr.press/v267/li25ds.html) | ICML 2025 正式论文 | 视觉表征会携带身份、场景和人口敏感信息 | Embedding 与人物图属于敏感派生数据 | representation leakage、删除、provider deny | 同行评审研究 | 任务定义不能覆盖所有本地隐私风险 |
| [Active Learning for VLMs](https://openaccess.thecvf.com/content/WACV2025/html/Safaei_Active_Learning_for_Vision_Language_Models_WACV_2025_paper.html) | WACV 2025 正式论文 | 在图像分类/prompt tuning 中研究不确定性与多样性选样 | 只能作为 question selection 候选 | active/random 同预算、分用户 strata | 同行评审研究 | 原任务不是私人检索或 Creator preference |
| [Relevance Feedback for CLIP](https://arxiv.org/abs/2404.16398) | ECCV 2024 workshop 工作 | 少量 relevance feedback 可调节 CLIP retrieval | 先学习轻量融合/adapter，不改基础 encoder | 相关/不相关反馈的 holdout 与 rollback | 同行评审 workshop | 证据强度低于主会；短期相关性不等于长期偏好 |
| [W3C PROV-O](https://www.w3.org/TR/prov-o/) | W3C Recommendation，2013 | Entity、Activity、Agent、use/generation/derivation 的互操作语义 | 内部建立轻量 provenance DAG | Schema round-trip 与 provenance query | 稳定标准 | 语义通用，不能定义媒体事实真实性 |
| [C2PA 2.4](https://spec.c2pa.org/specifications/specifications/2.4/specs/C2PA_Specification.html) | 正式规范 2.4，2026-04 | Signed manifest、assertion/actions/ingredients、hard/soft binding 与 AI disclosure | 只作为 export adapter，分开显示 binding/signature/trust | tamper/manifest removal/derived re-encode/size matrix | 活跃标准 | 验证声明和资产关联，不证明 claim 真实；SDK/平台支持持续变化 |
| [Content Authenticity Initiative SDK](https://opensource.contentauthenticity.org/) | 活跃官方开源 SDK；访问 2026-08-20 | 提供创建与验证 Content Credentials 的实现工具 | 用 adapter 隔离 SDK，内部 schema 不跟随其变化 | 容器兼容、签名、rollback、privacy、metadata overhead | 工程实现 | 证书、密钥、平台剥离与格式覆盖需单独治理 |
| [ALCE](https://aclanthology.org/2023.emnlp-main.398.pdf) | EMNLP 2023 正式论文 | 自动引用生成需同时评估 coverage 与 correctness | Story claim 的覆盖与蕴含分开 | 双人标注 claim/evidence | 同行评审研究 | 网页文本引用与多媒体证据不同 |
| [RAGChecker](https://proceedings.neurips.cc/paper_files/paper/2024/hash/27245589131d17368cccdfa990cbf16e-Abstract-Datasets_and_Benchmarks_Track.html) | NeurIPS 2024 Datasets & Benchmarks | 细粒度诊断 retrieval 与 generation 失败 | 分离 candidate/fusion/claim/compiler 分数 | 固定 candidate、Story Graph、Timeline 的阶段评测 | 同行评审 benchmark | RAG 指标需改造为 asset/span evidence |

#### 桌面安全与供应链

| 来源 | 状态 / 日期 | 官方结论 | MemoLens 推断 | 建议实验 | 成熟度 | 风险与外推边界 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [Electron Security](https://www.electronjs.org/docs/latest/tutorial/security) / [ASAR Integrity](https://github.com/electron/electron/blob/main/docs/tutorial/asar-integrity.md) | 官方 living docs；访问 2026-08-20 | Renderer sandbox/context isolation、IPC sender 校验、受限导航和 ASAR integrity 降低攻击面 | 现有 Electron 基线应保留，但 privileged payload 仍需 Spec 007 capability | malicious renderer、fuse/ASAR tamper、navigation/IPC matrix | 官方成熟指南 | 安全设置降低 compromise 概率，不能替代 renderer 失陷后的授权边界 |
| [Apple notarization](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution) | Apple 官方文档；访问 2026-08-20 | Developer ID、hardened runtime、notary scan 与 stapled ticket 组成直接分发链 | App、nested runtime、FFmpeg/helper 都进入签名/公证验证 | clean Mac 的 codesign/spctl/stapler/Gatekeeper | 平台正式机制 | 公证不是 App Review，也不证明业务安全或数据迁移正确 |
| [The Update Framework](https://theupdateframework.io/spec/) | 成熟开源规范；访问 2026-08-20 | Signed/versioned metadata 防 arbitrary、rollback、freeze、mix-and-match update | Updater 至少需要签名、版本单调、expiry/hash/size 与 anti-replay | invalid signature、rollback/freeze/mix-match/update interruption | 成熟安全规范 | 不要求直接集成 TUF；数据 schema rollback 仍是应用责任 |
| [SLSA Provenance](https://slsa.dev/spec/v1.1/provenance) | 正式供应链规范 v1.1；访问 2026-08-20 | Provenance 描述 artifact 在哪里、何时、如何由何 source/builder 产生 | Release artifact 绑定 source、builder、toolchain 和 digest | 两个隔离 runner 的 pre-sign payload/provenance 对照 | 成熟规范 | 签名 provenance 不能保证源码本身无漏洞 |
| [SPDX](https://spdx.dev/use/specifications/) | ISO/IEC 5962:2021；SPDX 3.x 活跃 | 标准化软件物料与许可证信息 | Electron/npm、Python、FFmpeg/native 和模型资源统一进入 SBOM/AI resource ledger | 解包 artifact 与 SBOM 100% 文件/依赖覆盖 | 国际标准 | SBOM 完整不等于依赖安全，模型权重/数据许可仍需专门字段 |

## 3. 成熟开源项目比较

| 项目 | 已验证做法 | MemoLens 可吸收部分 | 不应照搬 |
| --- | --- | --- | --- |
| Immich | OpenAPI 客户端、后台 job、独立 ML 边界、模型缓存、元数据/CLIP/OCR/人物检索 | 可恢复 job DAG、模型/索引版本、生成 contract、可观测重索引 | Redis/Postgres/多容器服务拓扑 |
| Ente | 端侧 ML、加密派生索引、跨 runtime golden parity、移动端性能分阶段测量 | 本地产生敏感 embedding；模型、预处理、schema 共同版本化 | 云同步和多设备加密拓扑 |
| PhotoPrism | API glue 与领域包分离、worker、元数据来源优先级、sidecar 规则、外部 vision adapter | 薄 route、可重建 projection、来源优先级与模型 manifest | 传统标签堆叠作为叙事记忆 |
| Nextcloud Memories | 保留用户文件结构，索引是派生能力，时间线/地图/转码围绕文件权限 | 文件系统为原始事实源，索引可删除重建 | Nextcloud 权限和应用生态耦合 |
| LanceDB | 嵌入式 multimodal/vector/FTS/SQL，适合本地试验 | 作为 bake-off 候选；评估 update/delete/version/restore | 未 benchmark 就作为新的权威数据库 |
| Qdrant | dense+sparse、多阶段 prefetch、named/multivectors、过滤与融合 | 检索实验参考，尤其多阶段和 multivector | 为桌面产品默认部署独立服务 |
| Weaviate | BM25F、vector、hybrid、named vectors、过滤 | 对照 hybrid contract 和异步索引 | 以服务运维复杂度换取尚未证明的规模需求 |

### 3.1 Immich

来源：[官方架构](https://docs.immich.app/developer/architecture/)、[项目仓库](https://github.com/immich-app/immich)。

**事实**：Immich 的 API、worker/后台任务、机器学习和持久化有明确边界。其客户端由 OpenAPI 生成；thumbnail、metadata、transcode、smart search 和 face recognition 都作为后台任务。ML 服务缓存模型，并把 Python/硬件依赖与主业务服务隔离。

**MemoLens 推断**：MemoLens 不需要复制容器和 Redis，但应复制任务语义：每个 ingest stage 可观察、可重试、可取消；模型或预处理版本变化触发显式 reanalysis；客户端不再手写大量 response cast。

**风险**：Immich 官方架构页也明确说明目标架构与当前代码可能不完全一致。它是服务器产品，直接搬到单机 Electron 会引入不必要的运维负担。

### 3.2 Ente

来源：[主仓库](https://github.com/ente-io/ente)、[端侧机器学习文档](https://github.com/ente-io/ente/blob/main/docs/docs/photos/features/search-and-discovery/machine-learning.md)、[ML 基础设施](https://github.com/ente-io/ente/tree/main/infra/ml)。

**事实**：Ente 把照片 ML 处理放在设备侧，派生 ML 数据加密后同步。其 ML 基础设施包含跨 Python、移动 runtime 的参考与性能工作。

**MemoLens 推断**：图片、caption、embedding、人物图和偏好都应被视为敏感派生数据。模型版本不能只记录名称，必须连同预处理、维度、dtype、runtime 和 input hash 一起固定。对桌面端不同硬件后端应做相同输入的 golden parity。

**风险**：官方基准的设备和样本范围不能直接外推至 MemoLens 用户库；模型代码与权重许可证要分别审查。

### 3.3 PhotoPrism

来源：[CODEMAP](https://github.com/photoprism/photoprism/blob/develop/CODEMAP.md)、[项目结构](https://docs.photoprism.app/developer-guide/directories/)、[项目仓库](https://github.com/photoprism/photoprism)。

**事实**：PhotoPrism 把 HTTP handler 定义为 glue，索引、媒体、vision、worker、entity/migration 等各自有包边界。元数据和 sidecar 规则有明确来源优先级。

**MemoLens 推断**：MemoLens 当前 `routes.py` 反向承载过多协调逻辑，应该向 application use case 下沉。Media、Atlas、Codex 不应各自猜测哪个字段更权威；来源优先级、修正和 projection 重建规则要成为领域合同。

### 3.4 Nextcloud Memories

来源：[项目仓库](https://github.com/pulsejet/memories)、[配置与索引文档](https://github.com/pulsejet/memories/blob/master/docs/config.md)。

**事实**：Memories 建立在 Nextcloud 文件与权限体系上，提供 EXIF 时间线、地图、回忆、AI 标签和视频转码，不要求用户放弃原有文件结构。

**MemoLens 推断**：MemoLens 的 SQLite 不应成为媒体事实的替代品。文件或系统媒体库是原始事实源；Asset ledger 记录身份和观察；Search/Atlas 是 projection。这样“重建索引”不会等同于“丢失记忆历史”。

## 4. 私人相册检索：PhotoBench 是最直接的外部基准

来源：[KDD 2026 官方 Papers 清单](https://kdd2026.kdd.org/papers/)、[DOI](https://doi.org/10.1145/3770855.3817511)、[arXiv 版本](https://arxiv.org/abs/2603.01493)、[官方仓库](https://github.com/LaVieEnRose365/PhotoBench)。KDD 官方清单将其列入 Datasets & Benchmarks Track；作者仓库在 2026-05-17 公布接收。

**事实**：PhotoBench 使用真实私人相册，查询覆盖视觉语义、时空元数据、社交身份和时间事件，并包含一对多结果、叙事型查询和无正确结果查询。论文报告两个关键现象：

- modality gap：统一 embedding 在精确的非视觉约束上失效。
- source fusion paradox：Agent 获得更多检索源后，可能因为错误计划或过强交集而退化。

官方仓库当前提供 300 条公开验证查询、887 条隐藏测试查询，以及隔离视觉表征与 Agent 规划的 protected variant。论文写 1,188 条 bilingual queries，当前 release 合计为 1,187，存在 1 条差异；MemoLens 必须以实际下载版本和 manifest hash 为准，不混用数字。

**MemoLens 推断**：这是目前与 MemoLens 最贴合的外部评测。项目应把查询编译成视觉、时间、地点、人物、关系、事件和否定约束；各来源独立召回；融合必须保留候选被排除的理由。单一 embedding 只能负责模糊语义，不能替代元数据和关系逻辑。

**实验候选**：

- 在 PhotoBench validation 上冻结现有 lexical、legacy hybrid、caption embedding 和 typed fusion 基线。
- 按单来源、双来源、三来源分别报告 nDCG/Recall/F1，不只看总平均。
- 对无答案查询报告误返回率和校准。
- 使用 protected variant 单独测 query planner，避免把视觉模型变化误认为计划能力提升。

**风险**：数据仅有少量相册，不能代表所有文化、语言、隐私或长尾库；完整数据集为 CC BY-NC 4.0，商业或发布用途必须单独确认许可证。它适合作为外部诊断集，不应取代项目自有合成和经同意私有 benchmark。

## 5. 长期记忆与时间修正

### 5.1 LongMemEval、MemoryAgentBench、BEAM

来源：[LongMemEval, ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/file/d813d324dbf0598bbdc9c8e79740ed01-Paper-Conference.pdf)、[MemoryAgentBench, ICLR 2026](https://proceedings.iclr.cc/paper_files/paper/2026/file/fd1eff9dd295df50a41f2521942fa31d-Paper-Conference.pdf)、[BEAM, ICLR 2026](https://proceedings.iclr.cc/paper_files/paper/2026/file/d7f0cfa0fe759b033d5262e1bb7d4065-Paper-Conference.pdf)。

**事实**：这些基准把长期记忆拆成信息提取、跨会话推理、时间推理、更新、拒答、测试时学习和选择性遗忘，而不是只测一次检索。

**MemoLens 推断**：Creator Memory benchmark 必须包含“后来修正了偏好”“某关系只在特定时间有效”“删除某个项目上下文”“旧结论应被替代但仍可追溯”。工作记忆、事件记忆、长期语义记忆和稳定偏好需要不同生命周期。

### 5.2 Graphiti/Zep、A-MEM、HippoRAG 2

来源：[Zep temporal graph 论文](https://arxiv.org/abs/2501.13956)、[Graphiti 仓库](https://github.com/getzep/graphiti)、[A-MEM](https://papers.neurips.cc/paper_files/paper/2025/file/19909c36f51abc4856b4560aff3d36d6-Paper-Conference.pdf)、[HippoRAG 2, ICML 2025](https://proceedings.mlr.press/v267/gutierrez25a.html)。

**事实**：Graphiti 把事实有效时间和系统观察时间分开，新事实使旧关系失效而不是物理覆盖。A-MEM 探索结构化 note、链接和演化记忆；HippoRAG 使用图式关联做多跳检索。

**MemoLens 推断**：Event、人物关系和 Creator preference 适合 append-only assertion，带 valid time、observed time、confidence、source、supersedes/invalidates。原始 observation 不被 LLM 改写。

**风险**：全量知识图谱会增加写入、查询和迁移复杂度。先用类型化关系表建立 baseline；只有在多跳、冲突、时间更新基准显著提升时，才引入更完整的图层。

## 6. 自适应检索：快路径与回忆路径

来源：[RF-Mem, ICLR 2026](https://proceedings.iclr.cc/paper_files/paper/2026/file/58f2612e86e2671432499017d049fcb0-Paper-Conference.pdf)、[WorldMM, CVPR 2026](https://openaccess.thecvf.com/content/CVPR2026/html/Yeo_WorldMM_Dynamic_Multimodal_Memory_Agent_for_Long_Video_Reasoning_CVPR_2026_paper.html)、[Interactive Episodic Memory with User Feedback, CVPR 2026](https://openaccess.thecvf.com/content/CVPR2026/papers/Subedi_Interactive_Episodic_Memory_with_User_Feedback_CVPR_2026_paper.pdf)。

**事实**：RF-Mem 使用熟悉度信号决定直接检索或更昂贵的迭代回忆。WorldMM 构建多尺度 episodic、持续更新的 semantic 和保留视觉细节的 visual memory，并由自适应 agent 选择。ReFocus 研究如何利用用户反馈改进 episodic temporal localization，也显示朴素使用反馈可能让部分基础方法变差。

**MemoLens 推断**：

- 高置信、单来源、约束已满足的查询直接返回。
- 只有低分差、高熵、来源冲突、约束未满足或多跳查询进入慢路径。
- 慢路径可以扩展事件邻居、改写查询、选择候选时间段、调用 MLLM reranker。
- 路由依据必须来自可校准信号，不能由 LLM 自报信心。

**实验候选**：至少 95% 简单查询停留快路径；困难查询 Recall@20 提升至少 15 个百分点；慢路径触发率低于 20%；若困难集提升不足 5 个百分点或总体延迟超过两倍，则取消慢路径。

## 7. 多模态与端侧模型

来源：[MM-Embed, ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/hash/6d5d6afa9957cfc9142ba60e78a467e9-Abstract-Conference.html)、[UniIR, ECCV 2024](https://www.ecva.net/papers/eccv_2024/papers_ECCV/html/11927_ECCV_2024_paper.php)、[MobileCLIP2, TMLR 2025 Featured](https://machinelearning.apple.com/research/mobileclip2)、[SigLIP 2](https://arxiv.org/abs/2502.14786)、[InternVideo2, ECCV 2024](https://eccv.ecva.net/virtual/2024/poster/1476)、[LanguageBind, ICLR 2024](https://proceedings.iclr.cc/paper_files/paper/2024/hash/2862ccf01e3843c81623b246895bcc45-Abstract-Conference.html)、[Matryoshka Representation Learning](https://arxiv.org/abs/2205.13147)。

**事实**：相关研究显示更大的 multimodal LLM embedding 不天然优于小型 CLIP 类模型；多语言、定位、多分辨率和端侧效率需要分别评估。InternVideo2 与 LanguageBind 探索视频、音频、图像和文本的共享表示。Matryoshka representations 支持在不同维度预算下使用同一表征。

**MemoLens 推断**：

- 一级召回使用便宜 bi-encoder 和结构化 filter。
- MLLM 更适合少量复杂候选的 query-aware rerank 或定向复核。
- Visual semantic、instance duplicate、text、audio 和 people-sensitive 表征保持独立 space。
- 对本地设备可实验可截断维度和多精度存储，但必须以项目数据的 Recall/latency/storage 曲线决定。

**风险**：公开 zero-shot 分数不能替代中文私人相册、OCR、低光、人物关系和近重复 burst 的项目 benchmark。InternVideo2 等大模型更适合研究轨道，不适合直接成为默认桌面依赖。

## 8. 长视频与时间证据

来源：[TimeChat, CVPR 2024](https://openaccess.thecvf.com/content/CVPR2024/html/Ren_TimeChat_A_Time-sensitive_Multimodal_Large_Language_Model_for_Long_Video_CVPR_2024_paper.html)、[LongVideoBench](https://proceedings.neurips.cc/paper_files/paper/2024/hash/329ad516cf7a6ac306f29882e9c77558-Abstract-Datasets_and_Benchmarks_Track.html)、[LongVU](https://proceedings.mlr.press/v267/shen25j.html)、[EgoLife](https://openaccess.thecvf.com/content/CVPR2025/html/Yang_EgoLife_Towards_Egocentric_Life_Assistant_CVPR_2025_paper.html)、[Ego4D Episodic Memory](https://ego4d-data.org/docs/benchmarks/episodic-memory/)。

**事实**：这些工作把 timestamp、temporal grounding、长上下文压缩和小时/天级生活记录作为独立问题。LongVU 通过视觉相似、文本引导和空间压缩减少长视频 token；Ego4D 使用 Recall@k 和 temporal IoU 衡量时间定位。

**MemoLens 推断**：视频证据必须是 `asset identity + [start, end]`，不能只指向整段文件。处理顺序应是镜头/场景/事件分段、音频/ASR/OCR/人脸/运动联合选样、segment 召回、query-guided refinement。所有摘要保留回到原始 segment 的反向边。

**实验候选**：与固定采样 baseline 比较 temporal R@1、tIoU、边界误差、token/关键帧减少量和 ingest wall time。压缩导致 Recall 降幅超过 5 个百分点时回退为分级自适应采样。

## 9. 隐私与主动学习

来源：[EgoPrivacy, ICML 2025](https://proceedings.mlr.press/v267/li25ds.html)、[Active Learning for VLMs, WACV 2025](https://openaccess.thecvf.com/content/WACV2025/html/Safaei_Active_Learning_for_Vision_Language_Models_WACV_2025_paper.html)、[Relevance Feedback for CLIP, ECCV 2024 workshop](https://arxiv.org/abs/2404.16398)。

**事实**：视觉表征可能泄露身份、场景和人口属性。主动学习工作使用校准不确定性与多样性选样，也显示朴素 entropy sampling 可能不如随机。少量 relevance feedback 可以不重训基础 encoder 而调整个人检索。

**MemoLens 推断**：Media Inbox 可升级为主动学习界面，但提问必须低负担且可撤销，例如人物合并、事件边界、相关/不相关和偏好二选一。先学习融合权重、规则和轻量适配器，不直接微调大模型。

**实验候选**：每个用户最多 30 次反馈；与 random sampling 同预算比较；个性化 nDCG@10 提升至少 10%；非个性化 holdout 回归不超过 2 个百分点。若 random 相同或更好，停止策略。

## 10. Provenance 与可验证创作

来源：[W3C PROV-O](https://www.w3.org/TR/prov-o/)、[C2PA 2.4](https://spec.c2pa.org/specifications/specifications/2.4/specs/C2PA_Specification.html)、[C2PA specifications index](https://spec.c2pa.org/specifications/)、[Content Authenticity Initiative SDK](https://opensource.contentauthenticity.org/)、[ALCE](https://aclanthology.org/2023.emnlp-main.398.pdf)、[RAGChecker](https://proceedings.neurips.cc/paper_files/paper/2024/hash/27245589131d17368cccdfa990cbf16e-Abstract-Datasets_and_Benchmarks_Track.html)。

**事实**：PROV-O 的基础实体是 Entity、Activity、Agent，并定义使用、生成和派生关系。C2PA 2.4 提供 signed manifest、assertion、actions、ingredients、hard/soft binding 和 AI disclosure。C2PA 明确不对 provenance 声明做“好或坏、真或假”的价值判断，它验证声明与资产的关联、格式和篡改状态。

**MemoLens 推断**：内部先建立轻量 provenance DAG。Story claim、shot、transition、music、render 都记录 source、时间段、模型、提示/规则版本、配置和用户确认。C2PA 只在 export/share 边界作为 adapter，不让 C2PA SDK 数据结构反向决定内部领域模型。

**实验候选**：Benchmark aggregate 引用覆盖率至少 98%，双人标注引用正确率至少 95%，所有导出镜头可追溯，冻结 threat matrix 的篡改分类正确率 100%。单个 publish-ready artifact 必须有 0 个 unresolved factual claim。Metadata 预算按媒体类型同时使用绝对和相对上限，不再用一个通用 2% 约束小图片与长视频。

## 11. 本地检索存储候选

来源：[LanceDB](https://github.com/lancedb/lancedb)、[LanceDB hybrid search](https://docs.lancedb.com/search/hybrid-search)、[Qdrant hybrid queries](https://qdrant.tech/documentation/search/hybrid-queries/)、[Qdrant multivectors](https://qdrant.tech/documentation/tutorials-search-engineering/using-multivector-representations/)、[Weaviate search](https://docs.weaviate.io/weaviate/search)。

当前不作选型结论。建议用同一 1k/10k/100k corpus 与 update/delete/rebuild workload 比较：

| 候选 | 值得测的原因 | 否决条件 |
| --- | --- | --- |
| SQLite FTS5 + brute-force vector | 已随本机 SQLite 可用，迁移与恢复最简单 | 真实 100k p95/RSS 不达标 |
| SQLite vector extension | 保持单文件和 transaction 语义 | 平台分发、签名或 ABI 风险过高 |
| Embedded LanceDB | multimodal/vector/FTS 和版本能力贴近本地产品 | 删除、升级、备份或包体不满足桌面要求 |
| USearch/HNSW side index | 轻量、可作为 projection | 增量一致性、重建和模型版本隔离复杂 |
| Qdrant/Weaviate | 功能丰富，适合作为上界对照 | 必须运行服务，资源与升级成本没有质量收益 |

评测必须固定 Recall、filter precision、p50/p95、build time、peak RSS、磁盘、update/delete、crash recovery 和可重建性。向量引擎是 adapter，不是 canonical ledger。

## 12. 七个可证伪假设

| 假设 | 最小实验 | 通过门槛 | Kill criteria |
| --- | --- | --- | --- |
| 类型化约束比单 embedding 更适合私人相册 | PhotoBench + MemoLensBench | nDCG@10 相对基线 +20%，无答案误返回 ≤5% | 提升 <5% 且复杂度显著增加 |
| 双时间 assertion 改善更新与冲突 | 200+ 时间/更新/多跳问题 | 准确率 +15pp，过期事实错 <2% | 不优于关系表 baseline |
| Familiarity routing 降低成本 | 简单/困难分层查询 | 简单 95% 快路径，困难 Recall +15pp | overall 延迟 >2x 或提升 <5pp |
| 分层时间记忆优于固定抽帧 | 100+ 私人视频问题 | tIoU/R@1 +20pp，token 减少 ≥90% 且 Recall 降 ≤3pp | Recall 降 >5pp |
| Evidence-first 创作提升可信度 | claim/shot 分层双人人审集 | aggregate coverage ≥98%，correctness ≥95%；单产物 unresolved factual claim=0 | 任一单产物带 unresolved factual claim 却进入 publish-ready |
| Capability ledger 可证明 local-first | network/permission/delete harness | 未授权 egress 0，删除闭包 100% | 只能靠 UI 提示，无法自动验证 |
| 主动学习优于随机反馈 | 固定 30 次反馈预算 | personalized nDCG +10%，holdout 回归 ≤2pp | random 相同或更优 |

## 13. 研究转化顺序

1. 先补评测和 capability，避免在不可验证、可外发的基础上扩张智能能力。
2. 再统一 Asset/Analysis ledger 和照片/视频 job，确保实验数据和 provenance 稳定。
3. 用 PhotoBench 和 MemoLensBench 选择 query compiler 与检索 adapter。
4. 分层时间记忆只在 segment benchmark 通过后进入默认路径。
5. Creator learning 先 shadow，只生成对比结果，不影响用户默认结果。
6. Story provenance 先实现内部 claim DAG，再评估 C2PA export adapter。

## 14. 明确不采用的研究误区

- 把论文报告的提升百分比写成 MemoLens 自身结果。
- 用一项公开 zero-shot 分数选择端侧模型。
- 默认部署完整图数据库或分布式向量服务。
- 把更多工具交给 Agent，却不记录候选集如何被裁剪。
- 用 LLM 摘要替代原始时间段和证据。
- 用 C2PA 证明故事语义真实。
- 让在线反馈直接改变不可解释的权重，且没有 holdout、回滚与用户撤销。

## 15. 对 Specs 的映射

| 研究结论 | 对应规范 |
| --- | --- |
| 质量、隐私、性能必须先可复现 | [004](specs/004-evidence-backed-retrieval-privacy-benchmark/spec.md) |
| 能力范围和 provider egress 要可证明 | [007](specs/007-local-capability-boundary/spec.md) |
| 原媒体、ledger、projection、job 分层 | [008](specs/008-unified-media-memory-kernel/spec.md) |
| PhotoBench、typed constraints、source fusion | [009](specs/009-intent-evidence-retrieval/spec.md) |
| Long video、episodic/event memory、adaptive recall | [010](specs/010-hierarchical-temporal-memory/spec.md) |
| 双时间偏好、主动学习、反馈回滚 | [011](specs/011-consentful-creator-model/spec.md) |
| PROV-O、claim coverage、C2PA export | [012](specs/012-verifiable-story-compiler/spec.md) |
| Electron、签名公证、更新、SBOM、clean machine | [013](specs/013-desktop-reliability-verifiable-release/spec.md) |

## 16. 最终判断

MemoLens 不需要追逐每一篇新论文。成熟开源项目应负责索引生命周期、contract、任务恢复和打包；前沿研究只进入带 baseline、隐私边界和 kill criteria 的实验轨道。

真正有机会形成代际差异的不是模型名单，而是系统能同时回答四个问题：它为什么找到这些素材；它为什么排除了其他素材；它从用户哪里学到了什么；它生成的每个故事片段来自哪里。后续 Specs 正是围绕这四个可验证问题组织。
