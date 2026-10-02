# Feature Specification: Live Agent Media Wiki Read Surface

- Feature ID：`ML-014-A0`
- 创建日期：2026-08-22
- 状态：`IMPLEMENTED / VALIDATED`
- 实施授权：用户已授权将 Grill Me 共识按小切片实现；本切片仅授权以下读能力
- 父规范：[ML-014 Agent-Navigable Media Wiki](../../spec.md)
- 产品决策：[43 问产品对齐记录](../../../product-decisions-43-questions-2026-08-22.md)
- 优先级：P0，Agent 创作链的首个可用读表面

## Overview

MemoLens 第一步不等待完整事件图、持久化 Wiki generation 或高级语义模型。它把已有的 canonical asset/source、当前成功视频分析 head 和已有 mixed search 投影成一个 Agent 可渐进阅读的、明确只读的媒体 Wiki 表面。

该表面解决一个当下问题：Codex、Claude 或其他 Agent 不应把整个媒体详情一次性塞进上下文，也不应自行扫描 Library。Agent 需要能执行：

```text
查看覆盖与缺口
  → 分页浏览素材页 / 用自然语言搜索
  → 打开 Asset 页或 Temporal Span 页
  → 解引用到 asset hash 或精确 [start_ms,end_ms) 证据
  → 知道哪些能力还没有
```

本切片是 live read model，不是完整 ML-014 中的物化 Wiki。响应必须返回 `projection.mode=live_read_model_v0`、`projection.generation=null` 和 `materialized_generation_unavailable` gap，不得伪称跨请求已固定 generation。

## User Scenarios & Testing

### User Story 1：Agent 先知道能查什么（Priority: P1）

**Given** 一个已建立部分索引的 Library，**When** Agent 读取 Wiki 状态，**Then** 它获得 asset 类型计数、当前成功视频 span 数、可用操作、已知缺口和快照边界，不把未分析说成没有素材。

### User Story 2：从地图逐层打开素材（Priority: P1）

**Given** Library 包含图片、音频和长视频，**When** Agent 分页浏览并打开某个 Asset 页，**Then** 它看到有界的元数据、review 状态、证据引用，以及视频当前成功分析中的 span 链接；默认浏览不包含 archived asset。

### User Story 3：从一句文稿到精确时间证据（Priority: P1）

**Given** 用户的一句口播稿，**When** Agent 执行 Wiki search 并读取一个视频结果的 evidence ref，**Then** 结果回到稳定 asset/source/hash、当前成功 analysis run/revision 和精确 `[start_ms,end_ms)`，不暴露绝对路径或原始媒体字节。

### User Story 4：不被文件名、OCR 或字幕中的指令欺骗（Priority: P1）

**Given** 文件名、描述或字幕含有“忽略之前指令”类文本，**When** Wiki 返回该数据，**Then** 响应显式标记 `untrusted_data_fields`，文本不改变工具权限、查询范围或 Agent 系统规则。

## Functional Requirements

- **FR-A0-001**：CLI 和 MCP 必须从同一 `MemoLensGateway` 暴露 `wiki-status`、`wiki-list`、`wiki-search`、`wiki-open` 和 `wiki-evidence` 的语义等价操作。
- **FR-A0-002**：五个操作在 safe-default 模式必须只读私有 SQLite DB/WAL 快照；不得 DNS、socket、Library 扫描、原始媒体打开或 SQLite 写入。
- **FR-A0-003**：每个响应必须声明 `projection_mode=live_read_model_v0`、`generation=null`、`cross_request_consistency=not_pinned` 与已知 gap；不得生成伪 generation ID。
- **FR-A0-004**：稳定 URI 必须使用 `memolens://library/current`、`memolens://asset/{asset_id}`、`memolens://span/{segment_id}`、`memolens://evidence/asset/{asset_id}` 和 `memolens://evidence/span/{segment_id}`。
- **FR-A0-005**：ID 仅接受非空、长度不超过 200 的受限字符集；拒绝 NUL、路径穿越、query/fragment、额外 URI authority 和未知 scheme/kind。
- **FR-A0-006**：`wiki-list` 默认只列出未 archived Asset 页，支持 image/video/audio 过滤、1–100 限制和不透明 cursor；该 cursor 只是 live keyset，不声称 generation pinning。
- **FR-A0-007**：`wiki-search` 必须复用已有 mixed search 候选和排序，不得伪称新的语义能力；图片结果指向 Asset 页/证据，视频结果指向 Span 页/证据。
- **FR-A0-007A**：`wiki-search` 的同一 Gateway/MCP 输入上限为 4096 字符；超限请求在排名前拒绝，避免无界 term 拆分、全库循环和回显。
- **FR-A0-008**：Asset 页必须返回稳定 identity、可用 source 引用、hash、基础媒体事实、review 快照和有界 span 链接；不返回绝对路径、Library root 或 DB 路径。
- **FR-A0-009**：Span 页和 span evidence 只允许解引用显式 current successful analysis head。Failed/running/历史非 current 片段在打开页面时必须返回 `wiki_page_not_found`，解引用证据时必须返回 `wiki_evidence_not_found`；不得通过 `MAX(revision)` 推测。
- **FR-A0-010**：Evidence 响应必须将 ID/hash/timing/head 标为 deterministic facts，将无法证明来自用户确认的 summary/semantic 标为 `model_hypothesis`；不得把情绪描述当作事实支持。
- **FR-A0-011**：页面正文、文件名、OCR、字幕和 semantic 文本必须作为不可信数据标记，不得被解释为工具指令。
- **FR-A0-012**：旧数据库如果有 mixed-media schema 但没有更高级 Wiki schema，本切片仍可用；如果连 mixed-media schema 都没有，status/list 必须返回结构化 `capability_unavailable`，不破坏旧命令。
- **FR-A0-013**：必须显式保留后续缺口：materialized generation、关系图、usage intervals、project pinning、scoped refinement、可移植 Wiki bundle 均不属于本切片。

## Contract Summary

| 操作 | 成功对象 | 主要输入 | 作用 |
| --- | --- | --- | --- |
| `wiki-status` | `memolens.wiki_status` | 无 | 覆盖、能力、缺口和读取边界 |
| `wiki-list` | `memolens.wiki_page_list` | kinds/limit/cursor | 小摘要分页，默认不含 archived |
| `wiki-search` | `memolens.wiki_search` | query/limit | 把现有 mixed results 变成可导航 page/evidence refs |
| `wiki-open` | `memolens.wiki_page` | page_id | 打开 Library、Asset 或当前 Span 页 |
| `wiki-evidence` | `memolens.wiki_evidence` | evidence_id | 解引用 Asset identity 或精确 current Span |

顶层响应共同包含：`schema_version`、`status`、`source`、`mode`、`projection`、`safety`。`projection` 固定表达 live read model 的限制，不使用时间戳或随机数伪造版本。

## Success Criteria

- **SC-A0-001**：Agent 可在不读取原始媒体、不输入绝对路径的条件下，完成 Library → Asset → Span → Evidence 的端到端导航。
- **SC-A0-002**：当前成功片段的 asset/source/hash/run/revision/时间范围与 SQLite 完全一致；failed 更高 revision 泄漏为 0。
- **SC-A0-003**：新增五个 CLI/MCP 路径在无 DNS、无 socket 和原 DB/WAL/SHM 字节不变测试中全部通过。
- **SC-A0-004**：递归扫描所有 Wiki 响应，绝对 Library/DB/cache 路径、provider payload、raw bytes 和 data URL 泄漏为 0。
- **SC-A0-005**：旧插件回归测试全绿；CLI 在非仓库 cwd 输出单一合法 JSON，stderr 为空。

## Non-Goals

- 不新增或修改 SQLite schema，不构建持久化 Wiki generation/head/page/evidence 表。
- 不新增 App UI，不更改导入、分析、编辑、渲染或导出路径。
- 不新增图数据库、embedding、LLM 调用、网络搜索或原媒体 refinement。
- 不声称改善检索准确率；本切片改善的是导航、上下文成本、证据定址和能力诚实性。

## Rollback

新能力只增加独立读取模块与适配层命令。回滚时删除新命令和模块即可；没有数据迁移、原件变化或持久化状态需要恢复。

## Implementation Evidence

- 共享 Core 合同已实现 `wiki-status/list/search/open/evidence`，CLI 与 MCP 只是同一 Gateway 的两个适配层。
- 视频 span 只在 `asset_analysis_heads + succeeded analysis_run` 同时成立时标记 current；legacy revision 只能降级，不伪称 successful head。
- 默认发现与精确证据的 source policy 分开：搜索优先 active/available，历史精确 span 在 root 失效时仍可解释并返回 gap。
- 当前共 22 个 Wiki 合同/安全测试，覆盖五操作主链、不可信文本、畸形 URI/干净 CLI 错误、大小写 data URI、Unicode/符号边界与带空格/单文件 locator 脱敏、正常日期/分类链保留、查询上限、RFC JSON、旧库降级、无网络/无原媒体打开/无 SQLite 写入、CLI/MCP 与嵌套 output schema。
- 当前合并验证：插件 90/90，后端单测 104/104，Node 51/51，renderer 43/43，TypeScript typecheck、全仓 Ruff、Python compile 与 `git diff --check` 通过。
- 未增加 SQLite migration、App UI、网络/模型调用、媒体上传、原件移动或任何写入能力。
