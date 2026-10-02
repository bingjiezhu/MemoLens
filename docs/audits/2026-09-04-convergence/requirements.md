# MemoLens 用户要求追踪：原始对齐、续档修正与当前缺口

日期：2026-09-04。范围：当前共享工作树的要求恢复与实现定位；本文件不是全产品验收证书，也不把父 Spec 授权等同于父 Spec 完成。本轮具体修复及新验证结果以同目录 `overview.md` 为准。

## 1. 取证范围与计数

只解析下列 JSONL 的 `type=response_item`、`payload.role=user`；没有把 `event_msg` 中的重复展示、assistant 推荐、工具日志或自动目标摘要当作用户新决定。`U` 是文件内所有上述 user record 的连续序号，包含环境注入记录；`L` 是原 JSONL 一起始行号，因此可以直接回查。

| 代号 | 原始文件 | 扫描行数 | user records | 环境/插件/自动目标注入 | 实质用户消息 |
| --- | --- | ---: | ---: | ---: | ---: |
| A | 2026-08-20 原任务（私有会话未发布） | 24994 | 74 | 3 / 2 / 1 | 68 |
| B | 2026-08-23 续任务（私有会话未发布） | 22579 | 12 | 1 / 1 / 1 | 9 |
| C | 2026-08-24 续档（私有会话未发布） | 3802 | 4 | 1 / 0 / 0 | 3 |
| D | 2026-08-29 至 09-04 续档（私有会话未发布） | 40131 | 6 | 3 / 0 / 0 | 3 |
| 合计 | 四份完整文件；D 以本次扫描时的末行为界 | 91506 | 96 | 8 / 3 / 2 | 83 |

83 条实质消息按首尾空白归一后有 74 种不同文本。唯一完全重复的文本是“继续”，共 10 次（A-U60/61/62/63/72/74、B-U4、C-U2、D-U2/3），属于多次续行授权，不能计为 10 个产品功能。B-U12 与 C-U1 是同一个 Grill Me 自问自答要求的近似重述，保留两处来源、只形成一个要求。43 问的 Hard 模式重复编号按正式决策文档合并为 Q2。

检查方式为先列出全部 user record 的序号、行号和文本长度，再分批读取实质消息；长消息完整展开。没有复制 credentials、完整工具输出或无关个人数据。来源 D 会随当前任务继续追加，以上是可重现的扫描截点，并非声称此后没有新消息。

本次还逐项对照 [43 问决策](../../specs/product-decisions-43-questions-2026-08-22.md)、[Portfolio](../../specs/portfolio-convergence-2026-08-23.md)、当前相关 Spec 与代码入口。没有穷举其他 MemoLens 任务或 2026-08-20 以前所有会话；因此“全部”指上述原任务及三份连续续档中可恢复的要求，而非无法证实的全账户历史。旧记忆摘要只用于找到源文件，状态均以原文与当前仓库重新定位。

## 2. 裁决优先级与状态用语

后续明确用户修正优先于早期回答；用户回答优先于 assistant 推荐；Portfolio 负责解释实施组合，不得凭组合优化删除用户产品目标。只有用户明确接受的推荐才是决定；未确认的产品设想、技术路线和质量提升数字不能因写进 Spec 而升级为用户要求。

时间边界已经明确：A-U2/L9 的“暂时代码先不改”只约束最初审计阶段；A-U57/L2964 明确要求开始实现共识并逐步写 Spec，A-U66/L19842 再次授权，A-U70/L23942 授权持续组合实施、调整/终止不合适旧 Spec 及可恢复退役旧目录。43 问顶部历史 `NONE` 不能覆盖这些后续授权。B-U9/L14251、B-U12/L22575、C-U1/L7、D-U6/L39944 继续该目标。实施授权不替代运行时用户素材 root、导出或外发的具体权限约束，也不是本轮 commit/push/发布授权。

本表状态含义：

- **implemented（部分）**：当前存在相应合同、服务或 UI；只说明代码落点，未在本要求恢复子任务重跑完整验收。
- **controlled-local**：相应切片记录了受控本地验证；继承该切片的场景和限制，不代表新鲜全仓验证、真实模型端到端、公开发布或父规范完成。
- **planned / experiment**：有要求或 Spec，但尚缺可交付实现，或尚未证明质量收益。
- **validation gap**：代码存在仍缺用户旅程、故障、真实 host/model、规模或发布证据。多个状态可同时成立。
- **开放 / 已否决 / 工作约束**：不应伪装成待实现功能，也不因未实现开放项判定产品违约。

## 3. 当前代码与规范定位索引

下列 E 编号用于矩阵复用。入口和测试文件的存在只用于定位责任与验证方式，不等于测试已通过。

| 编号 | 当前责任入口 | 对应 Spec / 验证入口与主要限制 |
| --- | --- | --- |
| E1 Library 与统一媒体账本 | [library_bootstrap.py](../../../backend/src/media/library_bootstrap.py)、[library_scan.py](../../../backend/src/media/library_scan.py)、[library_scan_persistence.py](../../../core/library_scan_persistence.py)、[media_db.py](../../../core/media_db.py)、[image_analysis.py](../../../backend/src/media/image_analysis.py) | ML-008-A0、015-A2；`tests/test_library_scan_runner.py`、`test_library_scan_persistence.py`、`test_library_bootstrap_runtime.py`。A2 有 Phase 2 全库扫描实现，不能再整体写成只有 Phase 0–1；主线程正修渐进调度与大批次预算问题，修复前后证据另记。 |
| E2 Wiki、检索与视频证据 | [memolens_wiki.py](../../../.agents/plugins/plugins/memolens/scripts/memolens_wiki.py)、[retrieval.py](../../../backend/src/media/retrieval.py)、[video.py](../../../backend/src/media/video.py) | ML-009/010/014-A0；现有代表帧、分段、sidecar transcript、live projection。`generation: null` 不等于 materialized Wiki、分析交换、完整 refinement/trace 或语义质量 benchmark。 |
| E3 Blueprint 与跨 Agent 合同 | [blueprint_contract.py](../../../core/blueprint_contract.py)、[blueprint.py](../../../backend/src/media/blueprint.py)、[memolens_cli.py](../../../.agents/plugins/plugins/memolens/scripts/memolens_cli.py)、[memolens_mcp.py](../../../.agents/plugins/plugins/memolens/scripts/memolens_mcp.py) | ML-015-A0/A1/B0/B1/B1A/B2A；现有持久化 revision/proposal、确认与恢复。research/technique/wiki pin 尚无完整可证明来源；不能把合同字段当作完整创意研究。 |
| E4 Coverage 与初剪编译 | [coverage_contract.py](../../../core/coverage_contract.py)、[coverage.py](../../../backend/src/media/coverage.py)、[timeline_lowering_contract.py](../../../core/timeline_lowering_contract.py)、[timeline_lowering.py](../../../backend/src/media/timeline_lowering.py) | ML-018-A1、015-B2B/B2B2；deterministic baseline、exact evidence、honest Gap；`tests/test_coverage_contract.py`、`test_timeline_lowering_integration.py`。全片全局优化、局部重规划与 paired preview 质量收益仍为 experiment。 |
| E5 Timeline 编辑与历史 | [timeline_edit_contract.py](../../../core/timeline_edit_contract.py)、[timeline_structural_edit_contract.py](../../../core/timeline_structural_edit_contract.py)、[timeline_restore_contract.py](../../../core/timeline_restore_contract.py) | ML-015-B2B3/B2B4C/B2C0；有 trim/replace/reorder、split/remove、append-only restore。`tests/test_b2b4c_shared_editor_integration.py`、`test_timeline_restore_service.py`。通用跨 Blueprint/Coverage/Timeline 的 undo/redo/branch 尚缺。 |
| E6 Codex 优先可视化插件 | [canonical-editor.html](../../../.agents/plugins/plugins/memolens/ui/canonical-editor.html)、[memolens_canonical_editor.py](../../../.agents/plugins/plugins/memolens/scripts/memolens_canonical_editor.py)、[memolens_editor_server.py](../../../.agents/plugins/plugins/memolens/scripts/memolens_editor_server.py) | ML-015-B2B4/A/B/B.1/C；Browser 工作台、素材证据、播放/seek、pending→Save→canonical reread。`test_canonical_editor_handoff.py`、`test_safe_canonical_playback.py` 在插件 tests 中。现有素材预览强制静音，不是 final-fidelity 成片证明；本轮图片刷新/播放状态/失败动作修复另记。 |
| E7 DeepSeek Harness 薄适配 | [deepseek-harness/README.md](../../../.agents/plugins/plugins/memolens/deepseek-harness/README.md)、[index.js](../../../.agents/plugins/plugins/memolens/index.js)、[cordis.patch.yml](../../../.agents/plugins/plugins/memolens/cordis.patch.yml) | ML-015-B2B4；插件 `tests/test_deepseek_harness.py`。README 记录两个固定 developer-preview 源码快照的 loader 验证；不据此声称所有版本兼容或真实 DeepSeek 模型已完成双向剪辑。 |
| E8 导出、使用事实与剩余区间 | [canonical_export.py](../../../backend/src/media/canonical_export.py)、[canonical_export_artifacts.py](../../../backend/src/media/canonical_export_artifacts.py)、[usage_projection.py](../../../backend/src/media/usage_projection.py)、[residual_identity_contract.py](../../../core/residual_identity_contract.py) | ML-017-A/A2/A3；当前切片记录 controlled-local，包含静音 1080p 硬切导出、exact usage、TXT/manifest、residual identity、成功输出 exact-SHA 排除。`tests/test_canonical_export_artifacts.py`、`test_usage_projection.py`。字幕/封面/有声混音/完整包/Usage correction 未完成。 |
| E9 创作者记忆与 Inbox | [creator_memory.py](../../../backend/src/media/creator_memory.py)、[inbox.py](../../../backend/src/media/inbox.py)、[memolens_creator_store.py](../../../.agents/plugins/plugins/memolens/scripts/memolens_creator_store.py) | ML-006/011/015；confirmed revision、状态操作与撤回已有基础；`tests/test_creator_memory.py`。Context Compiler 与提出完整遗忘承诺所需 Full Forget 未完成。 |
| E10 权限、外发与运行时 | [provider_egress.py](../../../backend/src/media/provider_egress.py)、[provider_egress_contract.py](../../../core/provider_egress_contract.py)、[production_surface_inventory.py](../../../core/production_surface_inventory.py)、[runtime.py](../../../backend/src/runtime.py) | ML-004-A0/A1、007-A0、013-A0；root/DB/source/egress 与生命周期已有分层实现；`tests/test_image_provider_egress_service.py`、`test_production_surface_inventory.py`。并非完整 Agent AnalysisRequest→ObservationBundle 协议或 clean-machine 发布证据。 |
| E11 历史桌面与视觉工作区 | [App.tsx](../../../src/App.tsx)、[VideoWorkbench.tsx](../../../src/VideoWorkbench.tsx)、[BlueprintProjectWorkspace.tsx](../../../src/blueprint/BlueprintProjectWorkspace.tsx)、[electron/main.ts](../../../electron/main.ts) | ML-005/006 历史总规范，015 UI 与 authority 实现。Electron 仍承载部分本地授权/生命周期；后续用户要求改变默认产品入口，不自动删除尚有 production caller 的代码。 |
| E12 待交付能力的规范入口 | [ML-014](../../specs/014-agent-navigable-media-wiki/spec.md)、[ML-016](../../specs/016-craft-wiki-technique-compiler/spec.md)、[ML-017](../../specs/017-export-usage-ledger-material-package/spec.md)、[ML-018](../../specs/018-script-coverage-global-footage-assignment/spec.md)、[Portfolio §5](../../specs/portfolio-convergence-2026-08-23.md#5-必须新增或重写的小切片) | 014-A1 generation、014-A2 analysis exchange、018-A0 Audio Timing、016-A0 Research Pin、Craft compiler、Open Project 与完整导出包等缺口。没有独立代码实现的要求明确保留 planned，不给不存在的入口。 |

## 4. 43 问逐号追踪

每一行保留一个正式编号。来源中的 A/B/C/D 均由第 1 节映射到原文件；简短“是的”由正式 43 问记录解释其主题，不扩大到同轮 assistant 的所有推荐。

| 问号 | 用户最终要求或裁决 | 用户来源（U / L） | Spec 与代码入口 | 当前判定与尚缺结果 |
| --- | --- | --- | --- | --- |
| Q1 | 个人自媒体创作者的长期私人素材库；补拍不是首要问题 | A-U11/1718、U12/1736 | ML-008/015/018；E1/E3/E4 | implemented（基础）；validation gap：冷启动到可发表作品的全链体验。 |
| Q2 | Hard 模式追问，推荐不能替代用户决定 | A-U13/1748、U14/1759 | 43 问使用规则；工作约束 | 已用于产品对齐；后续自问自答见 H08，不是软件内增加问答产品。 |
| Q3 | 不局限单一垂类；文字/口播与私人素材匹配是横向痛点 | A-U15/1771 | ML-009/018；E2/E4 | baseline implemented；跨题材真实匹配质量仍 validation gap。 |
| Q4 | 意图、已有文稿、指定素材、参考等多入口汇入完整创作链 | A-U16/1784、U35/2011 | ML-015/016/018；E3/E4/E12 | Blueprint/初剪 baseline implemented；热点、参考研究和专业建议链 planned。 |
| Q5 | 人掌握表达、立场、方向和最终发布；AI 做调研建议及执行；确认点可选 | A-U17/1796、U32/1977、U35/2011 | ML-015/016；E3/E10 | authority/确认合同 implemented；不能把历史三个确认点当固定强制向导。 |
| Q6 | 核心差异是管理沉睡素材、片段使用史与持续创作上下文 | A-U18/1808、U19/1820 | ML-008/011/014/017；E1/E8/E9 | usage/residual 有 controlled-local 切片；完整长期创作闭环仍部分。 |
| Q7 | 主动发现旧素材创作机会未被明确承诺首版 | A-U19/1820；43 问 Q7 | ML-014；E2 | 开放；不能把机会卡推荐或自动推送算成已采纳要求。 |
| Q8 | 原件默认不移动、删除、上传；归档必须确认、可验证、可恢复 | A-U20/1832 | ML-007/017；E8/E10 | 权限与非破坏路径 implemented；自动归档不是当前交付能力。 |
| Q9 | 不反推外部剪辑软件的素材使用；MemoLens 导出生成 TXT 即可 | A-U21/1844 | ML-017；E8 | 外部 NLE 反推已否决；TXT/usage 有 controlled-local 切片。 |
| Q10 | 成功导出时就记录具体素材和时间段使用 | A-U22/1856 → U23/1868 | ML-017-A；E8 | controlled-local；preview/失败不能增加 usage；父级有声成片闭环未完成。 |
| Q11 | 导出视频与 TXT 的简单流程；不先建复杂“确认发布”状态机 | A-U22/1856、U23/1868 | ML-017；E8 | 复杂发布流程已否决；不能以未接平台发布判定本要求缺失。 |
| Q12 | 尽量排除已做好的成片；原视频用过的秒数与可复用剩余画面分别记录 | A-U19/1820、U24/1880 | ML-017-A2/A3；E8 | controlled-local 的 exact residual 和 exact-SHA derivative exclusion；不等于识别任意转码后的外部成片。 |
| Q13 | 默认轻量包含成片/脚本/字幕/封面/清单/hash 引用；按需复制实际使用素材成完整包，原件不动 | A-U25/1892 | ML-017；E8/E12 | 部分 controlled-local：静音成片/脚本/TXT/manifest；字幕、封面、完整包 planned。 |
| Q14 | 一个本地 Library 文件夹即可；不以云或多来源为主线 | A-U26/1904 | ML-015-A2/008；E1 | implemented（分阶段）；多云联合库明确不作为当前主线。 |
| Q15 | 索引后主页先是素材库；未锁定机会卡首页 | A-U27/1916；43 问 Q15 | ML-014/015；E1/E2/E6 | 开放的机会卡不计 backlog；Library 状态与开始创作是现有验收重点。 |
| Q16 | 可用网上或用户自带样片，剪辑前动态搜索、整理并对齐参考 | A-U27/1916、U28/1930 | ML-016-A0；E12 | planned；外部 Agent 能浏览不等于 MemoLens 已有持久化 Research Snapshot。 |
| Q17 | 可解释技巧卡说明来源、适用素材、效果、自动化能力；选定后编译进当前剪辑 | A-U29/1941 | ML-016；E12 | planned；无已验证 Card store/Technique compiler，不能靠任意 prompt 声称实现。 |
| Q18 | 专业性覆盖内容匹配、节奏、声音、字幕、色彩；技巧适配文稿，支持视频/图片调色 | A-U29/1941、U30/1953 | ML-016/018；E4/E5/E12 | baseline 只能覆盖部分剪辑；音频智能/调色/专业质量仍 planned 或 experiment。 |
| Q19 | 学院派、有共识的拍摄剪辑与情绪知识；动态适配，保留创意 | A-U30/1953、U31/1965 | ML-016；E12 | Craft Wiki planned；不得把固定模板或未评测论文直接宣称为电影级能力。 |
| Q20 | 降低门槛同时帮助用户学习，AI 解释更好选择并处理繁琐实现 | A-U32/1977 | ML-015/016；E3/E6/E12 | 部分解释/证据界面存在；系统化专业知识渐进披露 planned。 |
| Q21 | 先快速出满意初版，再手动或对话精调；一键与共创是连续流程 | A-U33/1989、U35/2011 | ML-015/018；E3/E4/E5/E6 | deterministic 初剪及编辑 controlled-local/部分实现；真实多模态从意图到满意初剪仍 validation gap。 |
| Q22 | 可讨论也可一键，不强迫在选题和脚本逐次阻塞 | A-U35/2011 | ML-015；E3/E6 | 部分 implemented；高影响权限与版本确认不能被误呈现为多次创意审批。 |
| Q23 | 对话决定形成每项目个性化 Creative Blueprint | A-U36/2022 | ML-015-B0/B2A；E3 | implemented；Blueprint 有独立 revision；Coverage 拥有 assignments，聊天不是真源。 |
| Q24 | 事实自动记录、创作者记忆确认后沉淀、项目蓝图仅本项目生效 | A-U37/2033 | ML-011/015/017；E3/E8/E9 | 三层基础 implemented；完整 Context Compiler/Full Forget 未完成，禁止临时选择静默长期化。 |
| Q25 | 不先做复杂手工编辑保护区；每步有记录并能撤回即可 | A-U38/2045 | ML-015-B2C；E5 | 复杂保护区优先实现已否决；Timeline restore 只是部分满足。 |
| Q26 | 单操作回撤和 Google Docs/Slides 式连续历史 | A-U39/2057 | ML-015-B2C0/B2C；E5/E6 | Timeline 历史与 append-only restore implemented；跨对象统一 undo/redo/branch planned。 |
| Q27 | 普通对话入口，可直接接 Codex 插件，不强制表单向导 | A-U40/2069；B-U6/11787、U7/11838 | ML-015-A2/B2B4；E1/E3/E6 | 插件与 Browser 入口 implemented；不能继续把 Electron 主窗作为默认首用产品流程。 |
| Q28 | 必须有 ChatCut/OpenChatCut 式可视化工作台，与对话共用项目状态 | A-U41/2119；B-U5/11695、U6/11787 | ML-015-B2B4；E5/E6 | controlled-local 的 canonical editor；真实 host/model 双向及最终成片预览仍有 validation gap。 |
| Q29 | 常见剪辑功能和真实按钮优先，可借鉴成熟编辑器；延续 MemoLens UI | A-U42/2138、U48/2233；B-U5/11695 | ML-005/015/016；E5/E6/E11 | trim/replace/reorder/split/remove/play/seek 已有切片；不能据此声称字幕、音量混音、转场、调色已齐备。 |
| Q30 | 接受记录中的开源复用建议；实现前核实来源与许可证义务 | A-U42/2138、U43/2173；43 问 Q30 | ML-016/Portfolio §7；E5/E6 | 工作约束；43 问记录 OpenCut 可审计复用、OpenChatCut 只参考。此为历史裁决，不是本次实时许可证核验。 |
| Q31 | 不另造独立 AI 聊天/模型账户，外部 Agent 提供对话 | A-U40/2069、U44/2185、U55/2412 | ML-015；E3/E6/E7 | CLI/MCP/插件 implemented；不得新增第二套聊天 runtime 当成完成前提。 |
| Q32 | CLI 使 Codex、Claude 和任意可调用工具的 Agent 共享 Core | A-U44/2185；B-U8/13307 | ML-015；E3/E7/E10 | implemented；DeepSeek 固定 loader 快照验证有界；所有 Agent/版本兼容尚未证明。 |
| Q33 | 开放、可读、可迁移、Agent 无关的项目格式 | A-U45/2198；43 问 Q33 | ML-015 Open Project；E3/E12 | 内部合同/manifest 部分 implemented；可编辑逻辑包的 schema、迁移、relink、离线回读 planned。 |
| Q34 | file→scene/shot→usable segment；创作证据引用稳定具体时段 | A-U46/2209；43 问 Q34 | ML-010/014/018；E2/E4/E8 | 分段/精确 span implemented；更深 temporal graph experiment，不能用整文件摘要代替片段。 |
| Q35 | 正式记录采用带来源/时间/版本/未知状态的 observation，而非模型绝对真相 | A-U47/2220、U56/2444；43 问 Q35 | ML-010/014；E2/E10/E12 | **解释性归纳，非逐字明确接受**：U47 实际问的是音画文字与气口，见 H01。现有证据合同部分实现；完整 observation exchange planned。 |
| Q36 | 冷启动只需告诉 AI 照片视频在哪；不强制复杂五分钟演示教程 | A-U48/2233 → U49/2261 | ML-015-A2；E1/E6 | Bootstrap 已部分 implemented；clean-machine 一步安装/首次实际成片仍 validation gap。 |
| Q37 | 把要剪的素材放进文件夹即可开始；素材多要告知耗时，不能全库分析完才开始 | A-U50/2273 | ML-015-A2/014-A1；E1/E2 | 已有持久化扫描与统计；本轮已修渐进调度、预算恢复并通过回归，见 overview 的 C01–C03；精确 ETA 和全链大库体验仍需验证。 |
| Q38 | 长期 Library 加项目临时子目录/素材集合，仍保持统一身份 | A-U51/2285；43 问 Q38 | ML-008/015-A2；E1/E3 | identity/项目 scope 基础 implemented；子范围优先与全库分析并行的完整旅程需验收。 |
| Q39 | 不强制所有语义留本地；可抽关键帧等有界证据给大模型 | A-U52/2297、U53/2381 | ML-007/014/015；E2/E10/E12 | 本地预处理与 egress 部分 implemented；有界分析请求/回写/披露完整链 planned。 |
| Q40 | 本地做确定性处理；语义、创意、复杂匹配由 Agent 大模型承担 | A-U53/2381、U54/2396 | ML-014/015；E2/E3/E10/E12 | 本地语义优先已否决；legacy 本地模型路径存在不代表未来质量基线或可立即删除。 |
| Q41 | 先代表帧/拼图粗分析，项目时对少量候选定向放大复核 | A-U54/2396；43 问 Q41 | ML-014-A2；E2/E12 | 代表帧 implemented；panel→zoom→verified observation revision 的完整双阶段合同 planned。 |
| Q42 | 使用当前 Codex/Claude 能力，不要求用户再配视觉模型 key | A-U55/2412 | ML-015/014-A2；E3/E10/E12 | 插件复用 Agent 路线 established；不能以现有独立 provider 配置路径宣称无 key 全分析闭环完成。 |
| Q43 | 全库可持续理解的 Agent 可导航 Media Wiki，能搜、浏览、定位视频证据；参考 OriNodes/先进开源/论文 | A-U56/2444 | ML-014；E2/E12 | live Wiki A0 implemented；materialized generation/pinning、bounded refine/trace、全库覆盖和相对检索 baseline 收益未完成。 |

## 5. 43 问以外的原始要求及后续修正

| 编号 | 要求与来源 | 对应落点 | 本轮必须保留的判定 |
| --- | --- | --- | --- |
| H01 | **声音、画面、文字匹配与气口判断**：A-U47/L2220 | Portfolio 新增 018-A0 Audio Timing；ML-018；E2/E4/E12 | Q18/34/39–41 只能交叉引用，不能代替独立要求。当前 sidecar 字幕和音轨 probe 不等于 ASR、VAD、语义气口、waveform/onset、节拍或音画文字全局匹配。planned。 |
| H02 | **Codex 中直接安装插件并弹出可视画面**，以 ChatCut agent-plugin 为参考：B-U6/L11787 | ML-015-B2B4；E6 | 已有 canonical Browser editor，须证明真实插件进入、可编辑按钮、Save 后回读同一 canonical 状态；仅提供 MCP 文本结果不满足。 |
| H03 | **Codex 可视界面优先于 Electron**：B-U7/L11838 | ML-015-A2/B2B4；E6/E11 | Supersedes 早期 App/Electron 优先的产品入口。Electron 可保留确有调用的 runtime/authority，但 README/UI 默认路径必须转向插件；不代表可直接删除 Electron。 |
| H04 | **DeepSeek Harness 插件适配**：B-U8/L13307 | ML-015-B2B4；E7 | 这是已采纳兼容目标，不再是 43 问“未来 Agent 待定”。已有固定快照 loader/共同 MCP 合同；真实模型双向恢复/编辑与新版本兼容仍 validation gap。 |
| H05 | **真实剪辑 UI 与常用按钮**，持续参考 OpenCut/OpenChatCut：A-U41/L2119、U42/L2138、U48/L2233；B-U5/L11695 | ML-015-B2B4/C、016；E5/E6 | 当前精剪操作已有受控本地实现。A-U48 的“甚至直接用所有内容”是探索性表达，不能覆盖来源许可证和后来“提炼常见功能”的约束，也不授权完整复制受限项目。 |
| H06 | **写 Spec、按重要性逐步实现共识**：A-U57/L2964、U66/L19842；B-U9/L14251；C-U1/L7 | Portfolio V0–V8 与小切片 | 授权已存在；不能以最早 docs-only/NONE 重复索要实施确认；也不能把“所有 Spec 完成”解释为不判断价值而机械实现所有 FR。 |
| H07 | **全局架构收敛、可调整或终止旧 Spec、清理无用旧目录到可恢复垃圾桶**：A-U70/L23942 | Portfolio §2/3/8；ML-008/004/007/013；E1/E10/E11 | 清晰、专业、直觉和简洁是明确目标。移走旧路径前要有 successor、数据可回读、production caller 为零与 retirement manifest；本要求恢复子任务没有进行删除或宣称退役完成。 |
| H08 | **Grill Me 自问自答、据证据调整任务、先最重要再依次完成**：B-U12/L22575、C-U1/L7 | 本轮审计方法与风险排序 | 不是让每一步阻塞等用户回答，也不是把自问自答产生的新推荐冒充用户决定。应记录问题→证据→判断→行动→验证，独立审查承担挑战。 |
| H09 | **全面仓库逻辑/代码/目录架构检查，结合先进开源与学术资料，建议要有实质收益和全局视角**：A-U2/L9、U5/L1221、U56/L2444、U70/L23942 | ML-004 Evidence Gate；Portfolio；E10 | 新架构不能仅以新目录/更多 schema 作为提升。必须有失败复现、对比或用户可观察结果；论文或外部项目是候选依据，不是本项目验证结果。 |
| H10 | **经历多次中断后重审所有改动、恢复全部既有要求并体现在代码和 UI，合理分配 Agent/上下文**：D-U6/L39944 | 当前 convergence 文档与根任务修复 | 本文件负责追踪，主线程负责整合；扫描、UI、架构各自有明确责任与证据。文档整理和局部修复不等于全部历史父 Spec 实现完毕。 |
| H11 | **不自动启动 stop-slop**：A-U3/L1166；A-U4/L1176 要求列出自动技能 | 历史工作方式 | 用户未再指名时不能自动应用它；不能把历史使用该技能写成当前要求。不是软件功能。 |
| H12 | **与 HyperFrames 的区别**：C-U4/L3780 | 产品定位/现有方案解释 | 是比较问题，没有要求迁移到 HyperFrames 或把剪辑内核替换成该项目。不能从相邻技术讨论推导新实施目标。 |
| H13 | **历史提交请求**：A-U68/L23705 | 历史交付操作 | 证明当时有提交授权，不把它扩张为本轮自动 commit/push/远端发布授权。当前工作只记录原始要求。 |
| H14 | **素材包/节省空间设想的后续收窄**：A-U19/L1820 → U20/L1832 → U25/L1892 → U26/L1904 | ML-017/007；E8/E10 | 最终是一个本地目录、原件不动、轻量包默认、完整包按需复制；网盘、自动上传、不可恢复删除不是首版默认。 |
| H15 | **恢复之前 MemoLens 的 UI 设计美感与风格**：D-U7/L40280，本轮原统计截点后的新增消息；用户提供 [MemoLens GitHub](https://github.com/bingjiezhu/MemoLens) 作为无本地存档时的参照 | 关联 Q29、H03/H05；E6/E11；[视觉依据与恢复约束](visual-design.md) | 已据本地 v0.4/v0.5 真实截图和固定 commit 恢复浅色品牌外壳、低饱和 sage/暖铜金与浅深分区。真实 Codex Browser 桌面/窄屏视觉和合成项目分割保存/历史恢复通过，准确范围见 overview；这不是用户满意度或全产品完成证明。原 §1 统计快照不因本次追加而改写。 |

## 6. 不能漏记，也不能伪完成的下一步

以下是既有要求的未闭合部分，排列按用户主旅程及依赖；不意味着这些能力已在本轮实现。

1. **一个文件夹到首批可用素材**：扫描渐进调度/预算恢复修复已完成，局部回归证明首个 child 在扫描中完成与发布、提交后恢复不丢 remainder。下一步验证真实库 UI 状态与 durable job 一致及干净安装旅程，不能把局部回归升级为 A2 产品全验收。
2. **Agent 真正理解素材**：014-A1 Wiki generation/pinning、014-A2 有界分析交接和 observation 写回仍缺；当前可导航 live Wiki 和确定性分段不能代替语义理解质量。
3. **音画文字与气口**：为 H01 保留 018-A0 独立退出门槛，先区分本地声音信号、转写、语义停顿和来源映射。浏览器 source preview 即使传输了含 AAC 的 MP4，仍强制静音，不能证明音频编辑或有声导出。
4. **从文稿到更好初剪**：现有 Coverage/Timeline 是可追溯 baseline；全片匹配、局部重规划必须用同候选池 baseline 与完整 preview 的用户选择/编辑量比较证明收益，不能自称全局最优或电影级。
5. **插件内完整精修体验**：当前可视编辑器合成项目分割保存/历史恢复、版本化图片更新和失败下一步已局部闭合，见 overview；通用跨对象 undo/redo/branch、source-audio mix、字幕、基础转场、调色尚未齐备。固定 Harness loader 测试不等于真实 Codex/DeepSeek 双向模型旅程。
6. **成功导出的完整作品包**：A/A2/A3 只证明静音硬切、脚本/清单/manifest、exact usage/residual/derivative exclusion 的受控本地子集；Q13 要求的字幕、封面、完整素材包，以及音频成片仍缺。独立 Open Project 可编辑逻辑包也不能由 Export Package 替代。
7. **专业共创知识**：Research Snapshot/Reference Pin、Craft Wiki、Technique Card store 和 capability-verified compiler 仍为 planned；Creator Context Compiler、Full Forget 按原规范继续分期。
8. **退役与公开交付**：对 legacy image/brief/desktop 路径先完成调用者、迁移与回读证据再可恢复移走。clean arm64、签名/公证、hosted CI、真实 host/model 和最终保真预览残差没有通过时，保持 controlled-local 声明上限。

未采纳项单列保留：主动机会卡/自动推送、单垂类限定、云端联合库、外部 NLE 使用反推、复杂发布状态机、固定三阶段阻塞、复杂保护区优先、本地语义模型优先、独立视觉模型 key、独立 Story Graph/图数据库、自动 publish-ready 均不能悄悄回到首版默认。具体高阶技术可作为 experiment，但必须重新以需求和收益论证晋级。
