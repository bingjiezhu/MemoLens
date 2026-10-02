# MemoLens Spec Portfolio Convergence

- 日期：2026-08-23
- 状态：`DECIDED / STAGED IMPLEMENTATION AUTHORIZED`
- 产品决策源：[43 问产品对齐记录](product-decisions-43-questions-2026-08-22.md)
- 当前实现边界：[Next-generation Roadmap](roadmap.md)
- 适用范围：ML-004–018 的组合取舍、依赖、纵向切片、历史规范退役与结果声明

## 1. 组合裁决

ML-004–018 不是十五个并列 backlog，也不应按编号把每份父规范的全部 FR 依次实现。它们同时包含：

- 横切的证据、权限和发布门禁；
- 已实现但尚未迁移退役的历史总规范；
- 必须完成的用户纵向主链；
- 只能经预注册实验晋级的研究能力。

本轮裁决是：以用户能完成的纵向结果组合实施，而不以“做完了多少份 Spec”计数。新信息可以改变切片的内部方案，但不得静默破坏 [43 问决策](product-decisions-43-questions-2026-08-22.md#5-不可退回的产品原则)、原件不动、证据可回链、用户权威、历史可重放和诚实降级边界。

## 2. 唯一权威纵向链

```text
Canonical Media Ledger
  → Wiki Generation / exact evidence
  → Creative Blueprint Revision
  → Coverage Plan Revision
  → Timeline Revision
  → Export Revision
  → Usage Facts / residual intervals
```

正交输入是：

```text
Creator Context Revision
Research Snapshot
Technique Set Revision
Capability Profile
```

权威分工必须固定：

- **Canonical Media Ledger** 是素材、Source、Analysis、Operation 和导出 usage 事实的权威。
- **Wiki Generation** 是可重建、可固定的导航与证据 projection，不是可写事实源。
- **Creative Blueprint** 表达用户想说什么、立场、脚本、方向、约束、参考和用户决定；它不重复保存整片计算出的 assignments。
- **Coverage Plan** 拥有 Beat / Need / Candidate / Assignment / Gap 和全片权衡。
- **Timeline** 是经当前 capability 验证后的可执行剪辑 IR。
- **Export Revision** 固定实际渲染、source mapping、输出和完成状态；只有成功导出才产生 Usage Facts。
- **Operation Ledger** 只记录上述状态 head 的有意义变更、回撤、恢复和分支，不是第二份内容真源。

因此，legacy `brief/storyboard` 只可作为迁移输入或 projection；ML-012 的完整 `Story Graph` 不再成为一个独立 canonical 层。最小 claim/evidence taxonomy 直接进入 Blueprint、Coverage 和 Export provenance。

## 3. Spec 处置表

| Spec | 组合处置 | 必须保留的最小范围 | 不作为当前主线的范围 |
| --- | --- | --- | --- |
| ML-004 | 保留为持续 Evidence Gate | 004-A 黄金旅程、隐私、baseline、差异工件 | 完整公开研究平台分期 |
| ML-005 | 历史总规范，待迁移退役 | 保住已有 grounded evidence、typed Timeline、validation 和 preview | 不再按该大文档继续扩建；分流到 008/015/017 |
| ML-006 | 历史实现规范，待迁移退役 | Inbox 不动原件与 confirmed-only Creator Memory | Inbox 归 008/Library；Creator Memory 后续归 011 |
| ML-007 | 必须保留 / P0 | 007-A runtime、root/DB/source、egress 与 high-impact capability 收口 | 不先建通用 policy DSL/token 平台 |
| ML-008 | 必须保留 / 最高架构优先级 | canonical identity、durable analysis job、authority 分级、projection rebuild | 不大爆炸换库或换目录 |
| ML-009 | 保留 Core，高级路径实验 | 009-A typed intent/evidence/error、honest empty、hard/soft/unknown | 自由 LLM planner 与慢路径必须先 shadow |
| ML-010 | 能力内嵌 / 研究保留 | 精确 span、可修正 temporal observation、有预算 refinement 进入 008/014/018 | full event/episode/concept graph 不进入已承诺主线 |
| ML-011 | 必须分期 | 011-A Context Compiler；提供遗忘承诺前补 Full Forget | 主动学习、行为画像、opaque adapter 保持实验 |
| ML-012 | claim 能力内嵌 / 独立 Story Graph 暂停 | factual / interpretive / user-provided / stylistic、evidence、review-required | 独立 Story Graph、C2PA 主线、自动 publish-ready |
| ML-013 | 必须分期 | 013-A lifecycle；Public beta 前 013-B arm64 clean signed bundle | auto-update、完整供应链和 x86_64 按证据分期 |
| ML-014 | 必须保留 / P0 底座 | A0 已实现；后续 materialized generation/pinning、bounded refinement、traversal benchmark | 不默认引入独立图存储 |
| ML-015 | 必须保留 / P0 主链 | 已有 A0–B2A、B2B revision 1 与 B2B2 deterministic successor/inspection；新增 Library/Project Bootstrap，后续 B2C 和 Open Project slice | 不内置聊天、Agent scheduler 或第二模型账户 |
| ML-016 | 保留 / 解释先行、编译实验 | Research Snapshot/Reference Pin、Core Craft Wiki、Technique Card、capability-verified typed edits | 特效市场、参考复刻、自由效果代码 |
| ML-017 | 必须保留 / P0 闭环 | 017-A successful export + exact usage + lightweight package；A2 used/residual projection、Brief admission 与 polling；017-B full local package | 不做发布反推、云上传、自动移动/删除 |
| ML-018 | 必须保留 / P0 差异化实验 | Beat/Coverage baseline、Audio Signals、Global Assignment、local replan、paired preview benchmark | 不用一个审美分或固定模板替代用户选择 |

`Historical / pending retirement` 不等于已删除或可以立即删除。ML-005/006 只有在 successor contract 已落地、production caller 为零、数据/项目迁移通过、整链验收和 retirement manifest 完成后，才能从主目录移走。需要清理时应移到可恢复的垃圾桶/待退役区，不使用直接不可恢复删除。

## 4. 当前已实现与真实缺口

本节形成于 2026-08-23，以下两段保留当时的组合依据。此后 canonical image bridge、A2 Phase 0–3、exact residual/derivative exclusion 已有切片实现和本地验证，因此下方第 1、2、10 项不能继续解读为“当前完全无实现”。2026-09-04 的代码现状、剩余验收范围与原用户要求映射见 [全局复核](../audits/2026-09-04-convergence/overview.md) 和 [需求追踪](../audits/2026-09-04-convergence/requirements.md)；具体状态仍由对应切片证据支持。

### 已实现基础

- 本地媒体索引、基础视频分段、精确 span evidence 与 sidecar subtitle 读取。
- Creator Memory confirmed revision 与 Media Inbox 的 Keep/Archive/Favorite/Ready/Undo 基础。
- Legacy grounded brief、typed Timeline revision/diff/validation 与 720p preview/render 基础。
- Agent 安全默认只读的 Library/Wiki/project/Blueprint 导航。
- ML-014-A0 已实现并验证。
- ML-015-A0/A1/B0/B1 已实现并验证；B1A 本地 `LOCAL GO`。
- ML-015-B2A canonical Blueprint Workspace 已实现，但仍有 Desktop/视觉/hosted CI residual，不写成完整发布。
- ML-018-A1 deterministic baseline Coverage、ML-015-B2B first-cut lowerer、ML-015-B2B2 V9 deterministic successor/read-only inspection、ML-017-A canonical export/usage lightweight package，以及 ML-017-A2 Usage Search/Brief admission/polling 已在当前共享工作树实现并通过各自本地 focused gate。ML-017-A 已观察到真实 Electron native success；fresh V9/A2 Electron、Remote CI 与各自范围外能力仍保留为 residual。

### 主链缺口

1. 图片/视频尚未完全收敛到一个 Canonical Media Ledger 与 durable analysis lifecycle。
2. 尚无真正的“Agent 发起一次本地授权 → 连接 Library → 建立 canonical 新项目”冷启动纵向切片。
3. Wiki 当前是 `generation: null` 的 live projection，没有 materialized generation/pinning、refine/trace 闭环和复杂任务 benchmark。
4. 多模态 panel/short-span 交接只有分散约束，没有 Core-issued request → disclosure manifest → structured observation → analysis revision 闭环。
5. 当前只有 sidecar transcript；没有 ASR adapter、pause/breath、VAD、waveform/onset、music beat 与音视频独立 source mapping 的可交付信号层。
6. Core 尚不能证明 Research Snapshot、Technique Revision 和 Wiki Generation，因此 Blueprint 对这些 pin 保持正确拒绝。
7. B2B Blueprint/Coverage→Timeline revision-1 compiler、B2B2 deterministic Coverage reconciliation、B2B3 closed manual edit，以及 B2B4 Codex/DeepSeek 共用 paired Canonical Editor 已实现；final-fidelity preview、Timeline approval 与真实双向 host/model UI 验收仍未闭合。
8. B2C0 Timeline-local read-only history 与 append-only K→N+1 restore foundation 已实现；跨 Blueprint/Coverage/Timeline/UI 的 unified history、通用 undo/redo/branch/merge 仍未实现。
9. ML-018-A1 deterministic baseline Coverage 已实现；全局优化、局部重算与完整 preview paired benchmark 未实现。
10. ML-017-A canonical 本地导出、精确 usage ledger、`使用清单.txt` 与轻量包，以及 A2 used/residual Search、事务内 Brief admission 与 bounded polling 已实现；exact residual clip identity、derivative-output exclusion、Usage correction/supersession 与完整本地素材包仍未闭环。
11. Craft Wiki/Technique Card 没有可证明的知识与执行存储，也没有 capability-verified compiler。
12. Open Project 逻辑包的验证、迁移与 relink 尚未拆成实施切片。

## 5. 必须新增或重写的小切片

### 015-A2 Library & Canonical Project Bootstrap

- Agent 只提出用户意图；Desktop/main 签发准确 root authority。
- 一次原生确认后建立 Library identity、可恢复索引 operation、canonical project 与最小 Blueprint。
- 支持 Library 子目录作为项目优先范围，不产生第二份素材真源。
- Core 不可用时返回可恢复 action，CLI 不回退到直写 SQLite。

### 014-A1 Materialized Wiki Generation & Pinning

- 原子激活完整 generation；半成品不可成为 current。
- 项目固定 generation/analysis watermark；当前全局分析更新不改写旧项目。
- 渐进显示已覆盖、未覆盖、未知和优先项目范围。

### 014-A2 Bounded Multimodal Analysis Exchange

```text
Core-issued AnalysisRequest
  → exact asset/span + bounded frame panel/audio/short clip
  → disclosure manifest + capability check
  → structured ObservationBundle
  → Core validation
  → immutable analysis revision
```

Agent 不能直接把自由文本写成 Wiki 事实。协议必须固定 payload、Agent/model identity、output schema、authority、预算、保留、重试与 unknown。

### 018-A0 Audio Timing Intelligence

- 本地确定性信号：audio track、loudness、silence、waveform/onset。
- 当前 Agent 或用户供给的 observation：转写、语义停顿、speaker/voice 信息。
- breath/pause 是带来源和置信的 observation，不把单一 silence threshold 声称为“气口判断”。
- 音频和画面 source mapping 分离，为 J/L cut、ducking、保留原声和 voice-over 提供 typed 边界。

### 016-A0 Research Snapshot & Reference Pin

- 先支持用户给出的 URL/参考样片，不以自动热点抓取为前置。
- 固定 source、retrieved_at、引用范围、content digest、freshness、rights/usage 和项目用途。
- 研究结果只进当前项目，不写入私人媒体事实或未确认 Creator Memory。

### Timeline Capability Registry

- 首批只声明已验证的 hard cut、trim、replace/reorder、crop/reframe、audio level、subtitle、基础 transition 和 render mapping。
- B2B 是 validated Blueprint/Coverage/Technique/capability 到 Timeline 的 deterministic lowerer，不在编译器内另建自由素材 planner。
- Technique Card 只有映射到已验证 typed operation/validator/render fixture 时才声明“可自动实现”。

### Open Project Logical Package

Open Project Package 保存可继续编辑的逻辑状态；ML-017 Export Package 保存某次成片及它的使用制品。两者必须分开 schema、identity、migration、validation 和 relink 语义。

## 6. V0–V8 纵向交付顺序

| 阶段 | 组合范围 | 用户可观察退出门槛 |
| --- | --- | --- |
| V0 安全且可恢复地启动 | 004-A + 007-A + 013-A | 用户不输入 Python/FFmpeg/DB path；未授权 root/egress 为 0；重启无孤儿 writer；黄金旅程红/绿现状被冻结。 |
| V1 一个文件夹即可开始 | 008-A + 015-A2 + 014-A1 | 干净环境只确认一个 Library；建立 canonical project；深度分析未完成时首批素材可搜；kill/restart 后半成品不成为 current。 |
| V2 Agent 能看懂少量正确证据 | 009-A + 014-A2 + 018-A0 + 016-A0 | 文稿返回精确 asset/span；panel→zoom 可定向复核；每次外发有 manifest；无音频/语义能力时诚实降级；参考可固定到项目。 |
| V3 第一条可播放初剪 | 011-A + 018-A Coverage baseline + 015-B2B + Timeline Core | 一句意图/口播稿形成 Blueprint 与 8–20 Beat Coverage Plan；每个 clip 回链 Beat/Need/source span；可 preview 并 replace/trim/reorder/subtitle；gap 不用无关候选填满。 |
| V4 导出即闭环 | final export capability + 017-A | 成功导出同时得到成片、脚本/字幕、`使用清单.txt`、manifest；失败/preview usage 为 0；长视频剩余区间继续可搜。 |
| V5 对话与工作台是一条历史 | 015-B2C + 018 local impact/replan | Agent/UI operation 真实排序；undo/redo/restore/branch 不删历史；重放 digest 一致；locked 范围不被静默改写。 |
| V6 证明全片规划更好 | 018-B Global Assignment | 与 independent top-k、fixed de-dup、greedy diversity 使用同候选池；完整 preview 盲选和主动编辑数达到预注册门槛，否则只保留 shadow。 |
| V7 专业共创而非特效堆叠 | 016-A/B | 参考被拆成有来源/条件/风险/自动化等级的 Card；只编译已验证 typed edits；不支持的 Card 保持 explain-only。 |
| V8 可公开交付 | 013-B + 017-B；随后再考虑高级 010/011/012 | clean arm64 Mac 无开发依赖完成黄金旅程；签名/公证通过；完整包在 Library 离线时可验证；未过实验能力不进入公开承诺。 |

V4 结束前不应声称“首个完整产品闭环”。V6/V7 是质量跃迁，但必须建立在已可导出、可记录使用的基线之上。

## 7. 开源、交换格式与学术参考边界

- [OpenCut](https://github.com/opencut-app/opencut) 是 MIT 方向的重要编辑器候选，但官方当前正在 ground-up rewrite，并规划 Editor API、plugin-first、Rust core、MCP 和 headless。MemoLens 必须先固定自己的 Timeline/Capability/Identity contract，再通过可替换 adapter 复用，不把项目真源绑定在其改写中的内部实现。
- [OpenChatCut](https://github.com/NeuraSea/open-chat-cut) 当前为 BSL 1.1（Change Date 2030-07-21，之后切换为 AGPL-3.0-only）；只参考交互和机制，除非项目未来显式接受当时有效的许可义务，不复制、链接或 vendor 其代码。
- [OpenTimelineIO](https://opentimelineio.readthedocs.io/en/v0.16.0/tutorials/architecture.html) 适合作为 editorial interchange adapter；它不取代 Blueprint、Coverage、用户权威或 Operation Ledger。
- [LAVE](https://arxiv.org/abs/2402.10294) 支持“Agent 帮助执行 + 用户直接 UI 精修”的共创方向；MemoLens 的额外要求是两个表面必须共用一个 typed Core 与可重放历史。

外部系统、格式或论文只提供候选机制和 baseline，不是 MemoLens 已验证产品结果的证据。

## 8. 实施授权与声明边界

用户已于 2026-08-23 授权按本文调整后的组合顺序持续实施，允许在不背离已决产品原则的前提下收窄、合并、拆分或终止原 Spec 中证据不足的方案。该授权表示可以按 V0–V8 创建小 Spec/plan/tasks、修改产品代码并执行与风险相称的验证，不改写以下事实：

- 未实现的切片仍是 `PROPOSED/PLANNED`，不因 portfolio 授权变成 `IMPLEMENTED`。
- 未过本地门禁的实现不是 `VALIDATED`；未过 hosted CI、签名、公证和 clean-machine 旅程的版本不是已发布产品。
- 任何数据迁移、用户目录写入、导出、覆盖、完整包、移动/归档或删除仍必须经具体 Spec 的 scoped capability、故障注入和可恢复验收；不由“实施整个 portfolio”自动扩张为任意文件权限。
- 原始媒体默认永不移动、删除、覆盖或上传；历史目录的退役只能在迁移和零调用者证据完成后以可恢复方式进行。

每个切片仍必须产生准确的实施证据，并在用户可观察退出门槛全部通过后才升级状态。
