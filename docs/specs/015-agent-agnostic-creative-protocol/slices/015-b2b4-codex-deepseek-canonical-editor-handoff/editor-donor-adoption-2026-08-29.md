# Canonical Editor Donor Adoption Record

- 记录日期：2026-08-29
- 适用切片：`ML-015-B2B4` 及其后续 canonical editing dialect
- 当前结论：`REFERENCE-ONLY EXCEPT EXPLICITLY AUDITED MIT REUSE`
- 当前磁盘事实：本轮没有复制、vendor、链接或运行三个 donor 仓库的源代码；MemoLens 的 Timeline、authority、browser handoff 和 UI 实现继续由自身合同定义

## 1. 冻结的外部快照

| Donor | 审计 commit | 2026-08-29 看到的许可事实 | 本项目处理 |
| --- | --- | --- | --- |
| [OpenCut](https://github.com/OpenCut-app/OpenCut) | `400f097becba5db0fbc305d5a65348cb81c20356` | 根目录 `LICENSE` 为 MIT；README 同时说明当前仓库正在 ground-up rewrite，并建议当下使用 classic 版本 | 可以审计后按文件复用 MIT 代码，但当前 B2B4 没有复制其源代码；只吸收通用编辑器交互模式 |
| [OpenChatCut](https://github.com/NeuraSea/open-chat-cut) | `50db015d17818d23cbadef6bbc5405c5c1700c68` | 当前为 Business Source License 1.1；额外生产使用仅覆盖自然人的个人非商业用途；Change Date 为 2030-07-21，届时切换为 `AGPL-3.0-only` | 当前只参考公开架构与 UX，不复制、链接、vendor 或修改其代码；不得把它描述为“当前 AGPL”或商业友好依赖 |
| [ChatCut Agent Plugin](https://github.com/ChatCut-Inc/agent-plugin) | `2f391095fe81de4097ef9ce652d124689d4a3e49` | 仓库树未发现根级 `LICENSE` / `COPYING` / `NOTICE`；`codex/.codex-plugin/plugin.json` 声明 `GPL-3.0-only` | 只参考插件封装和 host 交互；不复制 skills、MCP 配置、媒体脚本、品牌素材或 hosted endpoint 逻辑 |

commit 由 `git ls-remote <repo> HEAD` 冻结。许可判断只约束上述快照；未来审计必须重新冻结 commit，并逐文件检查第三方组件和例外条款。

## 2. 已采纳的模式

采纳的是行为和产品模式，不是 donor 的项目状态或代码：

1. 剪辑页面应有可读时间标尺、playhead、缩放、clip 轨道、裁剪/图片时长手柄和明确的剪辑按钮。
2. Agent 对话与可视化编辑器是同一条工作流；Codex/DeepSeek 只负责打开界面，不能各自拥有一份 Timeline 真源。
3. 编辑采用 `Stage → Review → Save/Discard`。拖动可以预览，但只有显式 Save 才提交 exact CAS 的 closed command。
4. 替换素材必须让用户看见当前画面和已验候选画面；浏览器只能提交 `clip_id + assignment_id`，不能提交路径、asset locator 或自造证据。
5. Codex 插件使用标准 manifest、skills 和 MCP 入口；临时 loopback UI 是插件能力的一部分，Electron 工作台不是前置条件。
6. 同一 MCP 能力通过薄适配接入 DeepSeek Harness；host 名称、tool card 或聊天上下文都不成为 authority。

## 3. 明确不采纳的部分

- 不把 donor reducer、project document、database、undo history、hosted account/auth、provider job 或 render state 变成 MemoLens authority。
- 不连接 ChatCut hosted MCP endpoint，也不复用其账号、品牌、技能正文、FFmpeg bundle 或生成服务。
- 不因为 donor UI 有按钮就增加 MemoLens Core 尚未定义的 Split、Delete、Duplicate、Transition 或播放语义。
- 不让 browser-side preview、拖动几何或 donor-compatible JSON 成为 canonical Timeline。
- 不把代表性缩略图声称为视频播放、最终画质或导出一致性证据。
- 不在没有新 schema/lineage/impact contract 时复制 OpenCut Classic 或 OpenChatCut 的完整 NLE 状态模型。

## 4. MemoLens 权威映射

```text
Codex / DeepSeek host surface
  → memolens_canonical_editor_handoff(project_id)
  → bounded loopback browser session
  → server-derived Blueprint + Coverage + Timeline heads
  → one staged closed edit
  → explicit Save with exact head CAS
  → canonical operation + revision + paired receipt
  → canonical reread
```

对应实现边界：

- canonical projection、候选 eligibility、thumbnail reopen 和保存后重读：`scripts/memolens_canonical_editor.py`
- loopback bootstrap/session、cookie/origin/CSP、bounded HTTP routes：`scripts/memolens_editor_server.py`
- ruler/playhead/zoom/track/handles/inspector/candidate review：`ui/canonical-editor.html`
- Codex manifest、skill 和 MCP tool：`.codex-plugin/plugin.json`、`skills/use-memolens/SKILL.md`、`scripts/memolens_mcp.py`
- DeepSeek Harness 薄适配：`deepseek-harness/`、`index.js`、`client.js`、`prompt.js`
- canonical edit vocabulary 与事务 authority：MemoLens Core 的 B2B3/B2B4 command、revision、receipt 和 capability contracts

这里列出的路径均相对于 `.agents/plugins/plugins/memolens/`，Core 合同仍由仓库根目录的 `core/`、`backend/` 与对应 tests 冻结。

## 5. 后续复用门槛

未来若要复制 OpenCut 的 MIT 源码，必须先补一份逐文件 provenance 清单，记录 donor commit、原路径、本地路径、许可证头、修改内容和替代测试；没有该清单就继续采用独立实现。OpenChatCut 与 ChatCut Agent Plugin 在当前许可边界下继续是 reference-only，除非用户明确决定接受对应许可义务并单独完成法律/分发边界审查。

任何 donor adoption 都不得改变以下顺序：先定义 MemoLens closed command、authority、failure atomicity 和 recovery，再接 UI。一个看起来像剪辑器的按钮，如果不能形成可验证的 canonical operation，就不进入主界面。
