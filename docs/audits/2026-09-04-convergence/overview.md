# MemoLens 全局复核与续接入口

这是 2026-09-04 的历史审计；当前公开源更新与验证见 [2026-10-02 发布记录](../../releases/2026-10-02-readiness.md)。私有机器路径已脱敏，以下过去的浏览器/插件会话不是当前可访问入口。

日期：2026-09-04。状态：本轮全局复核、已定位缺陷修复与 UI 风格恢复完成；历史父规范仍按逐项矩阵保留未完成项。

导航：[逐项要求与原文](requirements.md) · [代码职责与依赖](architecture.md) · [旧版视觉依据与恢复约束](visual-design.md)。后续任务先读这四份入口，再进入具体切片；不要再次把整个历史无差别分发给每个 Agent。

## 本轮目标与证据基线

把用户原对话、43 问与后续修正逐项对应到当前代码和可见界面，修复有复现依据的断点，并明确仍未实现的产品能力。2026-08-30 的测试结果是历史基线，不能替代本轮改动后的验收。

- 工作区：`<repo-root>`
- 分支：`codex/next-generation-creator-loop`
- 基线 HEAD：`0caa2cf4afcb041150c131a41c4323a907dc6b6e`
- 本轮起点已有 150 个 tracked 文件变化，以及大量未跟踪实现、测试和切片文档。它们属于用户既有工作，不能用整仓回退覆盖。
- 历史本地门禁：Python 1104、插件 471、Renderer 129；生产 oracle 149。这里只记录来源，不声明本轮已重跑。

## 可中断的任务分工

| 责任 | 文件所有权 | 验收与交接 |
| --- | --- | --- |
| 主任务：全局整合 | 本文、README、Spec 索引和 roadmap | 对照需求、代码与 UI；维护问题状态；最后实际页面验收 |
| 历史恢复 | `requirements.md` | 分批扫描用户消息，43 问逐项映射，记录后续修正与来源 |
| 扫描调度 | `backend/src/media/video.py`、`library_scan.py` 和相关测试 | 首批分析不再等待全库扫描；批量字节限制不阻断后续扫描；关闭与重放不退化 |
| 插件界面 | `canonical-editor.html`、`client.js` 和相关测试 | 同一 clip 替换后新图可见；播放按钮正确；不可用状态给出可执行提示 |

2026-09-04 第一次审计因执行中断结束，当时各实现文件尚未落下修复。续接时已复核磁盘与进程，再按上述互斥文件范围分配任务。

## 已定位问题

| ID | 触发与影响 | 当前状态 |
| --- | --- | --- |
| C01 | 全库扫描与子分析共用单线程 executor；扫描返回前首批子任务无法运行，阻断渐进创作 | 已修复；独立 scan 单槽 + analysis 单槽；事件屏障证明扫描中首个 child 已完成并发布 current projection |
| C02 | 16 文件批次合计超过 import 字节上限，即使单文件合法也会重复失败，游标不能推进 | 已修复；数量与字节双预算的稳定前缀；超大单文件拒绝后继续；提交后恢复不丢 remainder |
| C03 | submit 计算 admission identity 时与 shutdown 交错，关闭后仍可能入队 | 已修复；最终关闭检查、去重、ticket 和 enqueue 使用同一锁区 |
| U01 | replacement 保存后 clip ID 不变，图片 viewer URL 不变，仍显示旧素材 | 已改版本化路径并通过真实 Codex Browser：N1→N2，稳定 clip ID，新请求计数 1→2，实际图片改变；旧/未来 revision 409，不能取历史数据 |
| U02 | 图片本地时钟已播放，按钮仍显示 Play | 已修复；真实 Browser 显示 Pause，播放头推进至 255 ms，随后终点回到 Play |
| U03 | 多种不可用状态统一提示 Refresh；DeepSeek 错误卡缺少恢复步骤 | 已按闭合原因码区分下一步，禁止回显不可信错误正文；插件门禁通过 |
| U04 | 整页近黑绿、技术面板和说明挤占工作区，丢失旧版视觉层次 | 已恢复浅色品牌壳、深预览、暖色编辑区；1280×720、Codex 可见面板 918×644、窄屏 390×844 / 320×740 已实际检查 |
| U05 | 旧视频 play Promise 或已隐藏图片的迟到失败，会暂停新片段 | 已加入播放代次/身份保护和隐藏源清理；独立复核无 P1/P2，23 行为 + 4 路由测试通过 |
| D01 | A2 切片已记录 Phase 0–3，但总索引与 roadmap 仍称 Phase 0–1 | 已同步历史证据范围；真实 Library→编辑链仍标待验收 |
| D02 | 43 问顶部的历史授权 NONE 与后续实施授权缺少直接导航 | 已补历史时间边界和后续实施授权入口 |
| D03 | README 仍称 Browser 只读、1080p output grant 未实现、当前已 Public beta；部分 Spec 索引仍全是 Proposed | 已逐项纠正；明确旧桌面兼容路径、已实现静音导出、受控本地状态和父规范剩余范围 |

## Grill Me 决策记录

- 检验点：扫描能提交子任务是否等于用户可渐进搜索？建议判断：不等于，必须证明扫描未结束时首批子分析已完成。原因：同一个单线程队列可让两项单测各自通过，却让实际用户等待全库。
- 检验点：保存成功是否等于界面显示保存后的素材？建议判断：需要验证稳定 clip ID 下 revision 与 preview 的一致性。原因：只检查服务端新 revision 无法发现旧图片仍留在页面。
- 检验点：代码很多是否足以证明所有要求都已满足？建议判断：每项要求需要代码入口、可观察行为和对应验收；设计中的能力继续列为待实现。原因：避免把长文档和大量测试误当成完整创作体验。
- 检验点：一次自动化浏览器成功是否覆盖 Codex 实际宿主？判断：不覆盖。独立 Chromium 图片重载成功，但 Codex Browser 中 N2 仍显示 N1，服务端请求计数也未增长；以目标宿主反例驱动下一版修复。
- 检验点：更清楚的界面是否需要放弃旧版美感？判断：不需要。复用真实旧版 token 与视觉层级，把检查信息渐进披露，不复用不适合剪辑器的巨幅营销标题或强制六步向导。

## 本轮验证记录（最终）

- 扫描/调度针对性原代码回放 5 个测试中 4 FAIL；修复后 9 个相关模块 74/74 PASS；追加提交后恢复测试后 scan + scheduling 26/26 PASS。使用临时 Library 和本地 `semantic_hash`，没有 provider 调用。
- 最终插件 476/476 PASS（212.752 s）；其中 Python wrapper 执行真实页面/DeepSeek 的 23 个 JS 行为测试，另有 4 个版本化 route 测试。23 个子断言不能再与 476 相加制造一个更大的测试数。
- Typecheck、renderer/Electron build、Node 192/192、renderer model 129/129、verify:local、全仓 Ruff 均通过；production negative oracles 149/149 PASS、0 skipped。
- Core/backend 全量 1112/1112 PASS（1383.505 s）；最终编辑器服务代码再由全新进程验证 B2B4 集成 44/44 PASS（59.188 s）。分项门禁对应 `npm run check` 的组成，但本轮不是再次调用一条 `npm run check` 命令；日志位于 `<TEMP_EVIDENCE_ROOT>`。
- Python 3.14 运行中仍有未关闭 SQLite 连接的 `ResourceWarning`，没有隐藏或关闭这些警告；测试通过不代表连接生命周期问题已消除。未在本轮无依据扩大为全仓资源重构。

### 真实 Browser 与证据上限

- 图片缓存探针使用生产 `EditorServerManager` 和测试 snapshot adapter，只在内存构造合成图片；证明目标 Browser 网络/像素更新，不冒充真实 Core replacement 保存或用户素材旅程。第一版同步清 src 失败已保留为反例，最终版本化 route 成功。
- 视频 QA 使用 `run_b2b4b_playback_qa_fixture.py` 的真实 app/repository/pairing/source proxy，媒体是本地生成的两段 H.264/AAC。播放跨第一段进入第二段、暂停在 5368 ms、缩放 96→112、回到 source 1000 ms 均实际观察；DOM `muted=true`。这是自动 QA native approval，不是真实用户点击批准，也不是听感或最终音频证明。
- 可编辑合成项目另行完成：移动暂存→Discard，N1 与 ledger 未变；Timeline 1000 ms 处 Split→Save，精确回读 N2 的 3 段；Inspect K1 不改 N2，Stage restore→Save 将原 2 段恢复为新 N3，N1/N2 保留。实际走生产 Core 与编辑服务，没有直接脚本写入 canonical head；源文件哈希不变，usage/export 为 0。分时刻机器证据见 [browser-edit-qa.json](browser-edit-qa.json)。这不包含真实 Library、语义匹配、同 Beat replacement 保存或人工权限批准的全链验收。
- 窄屏检查以 DOM 实际尺寸为准：可见 Codex 面板仍是 918×644，视口 override 实际作用于隐藏测试页；390×844 和 320×740 均 `scrollWidth == clientWidth`，已滚动检查 Timeline 与 inspector。检查后恢复默认视口，不以发出 resize 命令本身作为窄屏通过证据。
- 本轮没有使用真实用户 Library、没有 provider 调用、没有 commit/push/tag/release。旧版本图与新版本截图是设计/运行证据，不是用户满意度调查。

### 已安装插件同步

- 按 `plugin-creator` 的已安装本地插件流程保留 `memolens-local` marketplace，不改 marketplace 配置；最终版本为 `0.10.1+codex.20260905014258`。
- 使用 `/Applications/ChatGPT.app/Contents/Resources/codex` 0.153.3 完成安装。全局 npm `codex` wrapper 缺 vendor executable，是另一个环境问题，本轮没有擅自重装它。
- 安装根：`<codex-cache>/memolens-local/memolens/0.10.1+codex.20260905014258`。源/安装包 78 文件逐字节一致，checksum dry-run 无差异；安装包含 0 个 `.pyc`。最终安装包 JS 行为 wrapper 1/1（内含 23 JS tests）、DeepSeek 合同 6/6 PASS。
- 两个仅含生成字节码的未跟踪 `__pycache__` 目录已可恢复移至 `<TEMP_EVIDENCE_ROOT>`，没有删除源码或旧安装版本。缓存测试用 `-I -B`，避免再生成字节码。
- 当前已打开的浏览器展示合成验收项目；这是短期本地 QA 会话，不是长期用户项目入口。新建任务才能安全拾取更新后的插件工具和 skill；本轮没有代用户新建任务。

## 仍需明确验证的产品链

Library → 索引/观察 → Wiki 证据 → Blueprint → Coverage → Timeline → 可视精修 → Export → Usage/剩余片段。Codex 与 DeepSeek 是对话和插件入口，共用 Core；Electron 提供本地权限和进程管理。

真实素材完整旅程、外部 Agent 的有界语义分析写回、字幕/声音/转场/成片预览、跨资源历史、完整可迁移项目包，以及干净机器和远端发布，均须按各自事实单独判定。本轮不会通过改写状态把这些范围变成已完成。
