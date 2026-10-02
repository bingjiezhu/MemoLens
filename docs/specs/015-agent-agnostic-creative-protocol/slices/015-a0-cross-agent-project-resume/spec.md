# Feature Specification: Cross-Agent Project Resume Read Surface

- Feature ID：`ML-015-A0`
- 创建日期：2026-08-22
- 状态：`IMPLEMENTED / VALIDATED`
- 实施授权：用户已授权将 Grill Me 共识按小切片实现；本切片仅授权三个只读项目恢复能力
- 父规范：[ML-015 Agent-Agnostic Creative Protocol](../../spec.md)
- 产品决策：[43 问产品对齐记录](../../../product-decisions-43-questions-2026-08-22.md)
- 优先级：P0，“对话不是项目真源”的最小可用证明

## Overview

MemoLens 已经持久化 Creative Project、legacy creative brief 和 Timeline revision，但外部 Agent 目前只能单独列出或读取 Timeline，无法在没有原聊天的情况下恢复“这个项目在做什么、当前到哪里、用了哪些素材证据、还缺什么”。

本切片在不修改数据库、不创建项目、不修订 Timeline 的前提下，提供一个严格白名单的 Resume Capsule：

```text
project-list
  → project-open
      → legacy brief projection + latest observed timeline head
      → memolens://evidence/... references
      → project-history / timeline-get / wiki-evidence
```

它是现有数据的只读恢复投影，不是完整 Creative Blueprint、显式 project head 或可重放 operation ledger。响应必须使用 `legacy_brief_projection`、`latest_observed_timeline_head` 和明确 gaps，不得把现有 schema 没有记录的能力补写成事实。

## User Scenarios & Testing

### User Story 1：新 Agent 找到可继续的项目（Priority: P1）

**Given** 用户在之前的 Codex、Claude 或 App 中建立过多个项目，**When** 新 Agent 调用 `project-list`，**Then** 它获得稳定项目 ID、有界摘要、完整性状态和不透明分页，默认不把 archived 项目混入活跃列表。

### User Story 2：不依赖聊天历史恢复当前创作上下文（Priority: P1）

**Given** 新 Agent 只知道 project ID，**When** 它调用 `project-open`，**Then** 它看到用户目标/平台/节奏等 legacy brief 白名单字段、最新观测 Timeline head 摘要、可解引用的 asset/span evidence 和已知缺口，不需要导入原聊天 transcript。

### User Story 3：查看修订链而不吞入整个 Timeline（Priority: P1）

**Given** 项目有多个 Timeline revision 或 branch，**When** Agent 调用 `project-history`，**Then** 它获得按时间倒序的有界修订摘要、parent/brief binding 可用性、validation 和安全 operation 类型摘要；需要完整内容时再显式调用 `timeline-get`。

### User Story 4：损坏、旧库或不完整项目仍诚实可解释（Priority: P1）

**Given** 项目缺 brief、没有 Timeline、latest timeline digest 不匹配或数据库仅有旧 schema，**When** Agent 列出项目，**Then** 列表只声明 `presence_only|incomplete`；**When** Agent 打开项目，**Then** 它获得结构化 incomplete/capability gap。系统不静默回退到旧 revision，也不伪造 Blueprint/head/history 能力。

## Functional Requirements

- **FR-A0-001**：CLI 和 MCP 必须从同一 `MemoLensGateway` 暴露语义等价的 `project-list`、`project-open` 和 `project-history`。
- **FR-A0-002**：三个操作必须只读同一个私有 SQLite 快照；不得 DNS、socket、打开原媒体、扫描 Library、写 DB/WAL 或创建项目文件。
- **FR-A0-003**：输入 project/timeline ID 只接受长度 1–200 的受限字符集；cursor 必须是不透明且经过验证的值，不接受 SQL、路径、URL 或任意关系名。
- **FR-A0-004**：`project-list` 默认只列出 `draft|active`，可显式要求 `archived|all`，支持 1–100 限制和稳定 project-ID keyset cursor；缺 brief 的项目不得消失，必须标为 `incomplete`。列表不解析完整 brief/Timeline，所以同时存在两类修订的项目只能标为 `presence_only`，不得声称 integrity complete；Agent 必须用 `project-open` 验证。
- **FR-A0-005**：`project-open` 只读取指定 project，并在一个 SQLite snapshot 内组合 project、latest brief revision 和 latest observed timeline head。
- **FR-A0-006**：brief 只能返回经过类型、长度和数量限制的白名单字段；必须标记 `kind=legacy_brief_projection` 和 `is_creative_blueprint=false`，不返回 raw `brief_json`、candidate object 或 provenance。
- **FR-A0-007**：白名单至少覆盖现有 `goal`、`duration_ms`、`aspect_ratio`、`audience`、`platform`、`tone`、`pace`、`must_include`、`must_exclude`、`narrative_arc`、`missing_assets` 和 `assumptions`；未知字段必须丢弃。
- **FR-A0-008**：latest observed Timeline head 的选择必须先对每个 timeline ID 选最高 revision，再按 `created_at DESC, revision DESC, timeline_id ASC` 选一个；响应必须声明 `authoritative_project_head=false`。
- **FR-A0-009**：最新观测 revision 即使 invalid、JSON 损坏、digest 不匹配或 brief binding 缺失也不得静默回退；必须返回该 head identity 和明确 gap，不返回不可信的 format/count/evidence/operation 派生摘要。`project-open` 还要求 binding 指向 latest brief 才能派生；`project-history` 允许从绑定仍存在的历史 brief 派生。
- **FR-A0-010**：Timeline 摘要必须校验 stored SHA-256，且只返回 ID/revision/hash/schema/format/track/clip/validation/created-at 等白名单字段；完整 Timeline 继续由现有 `timeline-get` 显式读取。
- **FR-A0-011**：Resume evidence 只从已验证的 Timeline clip 和可识别的 legacy candidate ref 提取 allowlisted asset/span ID，输出 `memolens://evidence/asset/{id}` 或 `memolens://evidence/span/{id}`；不得返回候选 raw payload。
- **FR-A0-012**：`project-history` 返回指定项目所有 Timeline branch 的有界修订摘要；只允许从 provenance 提取经白名单验证的 operation type 和稳定 subject ID，不得返回自由文本、raw provenance 或伪称完整 typed diff/operation chain。
- **FR-A0-013**：项目 title、brief 文本与任何来自 JSON 的自由文本必须标记为 untrusted data，不能成为指令、路径或 capability 输入；Timeline format 只允许有界数值和严格非可执行颜色语法。
- **FR-A0-014**：所有响应递归检查后，绝对 DB/Library/cache 路径、provider payload、data URL、raw bytes、raw brief/timeline/provenance 泄漏必须为 0；locator 脱敏不得被相邻边界字符、大小写 scheme、空格、Windows drive 或 UNC 形式绕过。
- **FR-A0-015**：完整 canonical `creative_projects + creative_briefs` 是 project resume 的最低条件；旧库必须返回结构化 `capability_unavailable`，现有 Timeline 读能力不得因此回退。
- **FR-A0-016**：若 `timelines` 和兼容性 `timeline_revisions` 同时存在，必须沿用固定优先级：完整 canonical `timelines` 优先，否则才读 `timeline_revisions`；禁止合并两个真源。
- **FR-A0-017**：响应必须显式保留 `creative_blueprint_unavailable`、`explicit_project_timeline_head_unavailable`、`project_write_unavailable`、`complete_operation_ledger_unavailable` 和 `wiki_generation_unpinned` 等实际缺口。

## Contract Summary

| 操作 | 成功对象 | 主要输入 | 作用 |
| --- | --- | --- | --- |
| `project-list` | `memolens.project_list` | status/limit/cursor | 找到可继续项目及其完整性 |
| `project-open` | `memolens.project_resume` | project_id | 返回一个有界、可解引用的 Resume Capsule |
| `project-history` | `memolens.project_history` | project_id/limit | 返回 Timeline revision 和安全 operation 类型摘要 |

三个响应共同包含 `schema_version`、`status`、`source`、`mode`、`safety`、`gaps` 和 `next_actions`。`project-open` 的有效 capsule 包含：

- project identity/status/timestamps；
- `legacy_brief_projection`；
- `latest_observed_timeline_head`；
- 稳定 Wiki evidence URI；
- 完整性、选择策略与已知 gap。

## Success Criteria

- **SC-A0-001**：新 Agent 仅使用 project-list/open/history 和现有 Wiki/Timeline 读工具，在无聊天 transcript 时正确定黄金 fixture 的 latest brief、latest observed Timeline head、validation 和 evidence refs，一致率 100%。
- **SC-A0-002**：多 Timeline branch 时严格按“各 branch 最高 revision → 最近 created head”选择；不得错把项目内数字最大 revision 当 project head。
- **SC-A0-003**：损坏的 latest head 不回退；digest/JSON/validation/binding 错误时派生 format/count/evidence/operation 泄漏为 0；无 brief、无 timeline 和旧 schema 都通过独立降级测试；`project-list` 不把仅存在的行误报为已验证完整。
- **SC-A0-004**：三个 Gateway/CLI/MCP 路径在单快照、无 DNS/socket、无原媒体打开、原 DB/WAL/SHM 字节不变测试中全部通过。
- **SC-A0-005**：递归检查所有新响应，私密路径/provider/cache/raw JSON/data URL 泄漏为 0；所有用户/模型文本均显式标记 untrusted。
- **SC-A0-006**：新 CLI 在非仓库 cwd 输出单一合法 JSON，stderr 为空；MCP 输入/输出 schema 与实际 payload 严格一致。
- **SC-A0-007**：现有 plugin、unit、node、typecheck 回归全绿，`timeline-list/get` 语义不变。

## Non-Goals

- 不新增 SQLite migration、Creative Blueprint table、project head table 或 operation table。
- 不创建、修改、撤销、恢复、分支、导出或渲染项目。
- 不修改 App UI，不建立新聊天，不保存 Codex/Claude transcript。
- 不把 legacy brief 重命名为 Creative Blueprint，不宣称已有完整 operation replay。
- 不复制或移动原件，不调用模型或网络，不固定 Wiki generation。

## Rollback

新能力是独立只读 adapter/presenter 与三个新入口。回滚只需移除新入口和模块；无 migration、持久化状态、原件变化或项目修订需恢复。

## Implementation Evidence

- 共享 `MemoLensGateway` 已实现 `project-list/open/history`，CLI 与 MCP 只是同一读合同的两个适配层；status 只广告当前 schema 能证明的能力。
- 每次操作只创建一个私有 SQLite snapshot；canonical `timelines` 优先，否则才使用固定 `timeline_revisions` adapter，不合并真源。
- `project-list` 只返回 `presence_only|incomplete`；`project-open` 通过 brief/Timeline digest、严格 JSON、identity、validation 和 brief binding 联合验证完整性。
- latest observed head 严格执行“每 branch 最高 revision → `created_at DESC, revision DESC, id ASC`”；损坏、invalid、dangling/stale binding 都不回退，也不派生 format/count/evidence/operation。
- 共享 locator 安全模块同时保护 Resume 和 Wiki，覆盖 Unicode/符号边界、带空格 POSIX、根目录单文件、Windows drive、UNC 及大小写 file/data URI，并保留日期、分类链和 `A / B` 等正常创作文本。
- 30 个 Project Resume 专项测试和 22 个 Wiki 专项测试通过；插件全量 90/90、后端 104/104、Node 51/51、renderer 43/43 通过，TypeScript typecheck、全仓 Ruff、Python compile、manifest JSON 和 `git diff --check` 通过。
- 独立安全/合同复审重跑 invalid/dangling/stale/path/presence/background/MCP/CLI 攻击样例后未发现 P0/P1/P2，明确准许标记 `VALIDATED`。
- 未增加 SQLite migration、App UI、网络/模型调用、原媒体打开、项目写入、渲染、导出或用户文件改动。
