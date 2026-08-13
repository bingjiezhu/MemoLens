# Spec 006：Creator Memory & Media Inbox

- 状态：`IMPLEMENTED`
- 目标版本：MemoLens `0.5.0`
- API 路径：`/v1`（响应 `schema_version: "1"`）
- 数据库目标版本：`3`
- 最后更新：2026-08-12
- 依赖：Spec 005《Video Creative Workbench》；Spec 004 仍是检索效果与隐私结果型声明的发布门槛

本文使用 RFC 2119 风格术语：**MUST（必须）**、**MUST NOT（禁止）**、**SHOULD（应该）**。

## 1. 产品北极星

MemoLens 是面向个人创作者的本地照片与视频素材库：用户只负责把素材存进已批准的目录，MemoLens 负责持续索引、轻量整理、记住已确认的创作偏好，并在用户准备发布内容时，让 App 或 Codex 从真实素材中找到证据、形成故事和可编辑初剪。

系统 **MUST NOT** 新增独立 Agent runtime。职责边界固定为：

- MemoLens App：唯一的素材状态、权限、确认和持久化所有者；
- Codex：创意推理与自然语言交互表面；
- MemoLens Plugin：Skill + 本地 stdio MCP，只暴露窄、可审计的只读工具；
- Flask API：桌面 App 与非 Codex 客户端的兼容接口。

这与 OpenAI 插件架构的“小型 Skill + MCP，只有检查、编辑、确认时才需要 UI”一致。MemoLens 不再要求用户为 Codex 路径额外配置模型 API。

## 2. 研究转化原则

用户提供的“去留照片 App”内容体现了两个值得吸收的原则：

1. 照片首先承载记忆，不是一堆待清理文件；整理应成为回忆与创作过程中的顺手动作。
2. 同时看见一组同日、相似或相关素材，比逐张进入详情更轻松，也更容易发现素材之间的叙事关系。

MemoLens **MUST** 吸收原则而非复制界面：

- 首屏不以“释放多少空间”为主要价值；
- Inbox 以同日/相似/创作相关批次呈现，仍提供文字按钮和键盘入口；
- 第一阶段只修改可撤销元数据，**MUST NOT** 删除、移动或覆盖原文件；
- Archive 的含义是“默认不参与 MemoLens 创作建议”，不是系统相册删除；
- 任一手势都 **MUST NOT** 成为唯一操作方式。

## 3. 信息架构

保留 `Home / Library / Memories / Create`，不增加第五个顶级页面。

- Home：已建库用户看到今日入口、待整理数量、最近作品和创作者记忆摘要；首次用户保留连接引导。
- Library：连接、索引和统一 Media Inbox。照片与视频都在这里进入素材库。
- Memories：主题、事件、地点和故事发现，不承担运行时配置。
- Create：从一句话到素材证据、Photo Story 或 Video First Cut。
- Creator Memory：作为 Home 摘要、Library 可编辑面板和 Create 的可见上下文存在，不成为独立导航页。

## 4. Media Inbox

### 4.1 状态模型

审阅状态绑定内容身份 `asset_id`，不绑定路径。每次更改写入完整不可变快照：

```sql
CREATE TABLE asset_review_revisions (
  asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
  revision INTEGER NOT NULL CHECK(revision >= 1),
  inbox_state TEXT NOT NULL CHECK(inbox_state IN ('inbox','kept','archived')),
  favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN (0,1)),
  project_ready INTEGER NOT NULL DEFAULT 0 CHECK(project_ready IN (0,1)),
  note TEXT,
  provenance_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  PRIMARY KEY(asset_id, revision)
);
```

无审阅记录等价于 revision `0`、state `inbox`。状态语义：

- `Keep`：保留在默认发现与创作检索中；
- `Archive`：默认不进入发现与创作检索，但原文件不变，仍可在 All/Archived 找回；
- `Favorite`：长期正向标记，与本次创作选择无关；
- `Ready`：创作者明确认为适合进入后续作品；
- `Later`：保持 Inbox，不写无意义 revision。

所有写入 **MUST** 使用 `BEGIN IMMEDIATE` + `base_revision` CAS；冲突返回 `409 review_revision_conflict`。撤销通过写回上一快照完成，不删除历史。

### 4.2 列表契约

`GET /v1/inbox` 支持 `state`, `kinds`, `limit`, `cursor`。响应只包含稳定 ID、媒体类型、文件名、拍摄时间、尺寸/时长、缩略图 URL 与当前审阅快照；**MUST NOT** 返回绝对目录、数据库 UUID、原始 transcript 或 provider payload。

`PUT /v1/inbox/assets/{asset_id}` 需要桌面 session token、`Idempotency-Key` 和 DB binding。它只能修改以上元数据，不能操作文件。

默认 mixed/photo retrieval **MUST** 排除 archived 资产；显式 ID 解析仍可返回 archived 项，并标明状态，避免历史 timeline 失效。

## 5. Creator Memory

### 5.1 透明、确认后学习

Creator Memory **MUST NOT** 从停留时长、一次点击、原始聊天或模型猜测中静默学习。来源优先级：

```text
用户手动固定 > 用户确认的建议 > 尚未确认的本地观察
```

主档案复用 creative brief 的稳定词汇：平台、受众、默认时长、画幅、语气、节奏、叙事弧、必须包含与排除。每个 revision 保存内容 hash 和证据引用：

```sql
CREATE TABLE creator_profile_revisions (
  profile_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision >= 1),
  profile_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL CHECK(source IN ('user_edit','confirmed_suggestion','reset')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(profile_id, revision)
);
```

档案保存在当前媒体 DB 中，切库不会泄露或合并偏好。Reset 写入新的空 revision，不擦除历史。

### 5.2 本地建议

`GET /v1/creator/profile/suggestions` 只读地聚合已保存 creative brief 与确认过的审阅状态。只有至少两个独立项目支持同一值时才提出建议；GET **MUST NOT** 写档案。建议展示字段、候选值、证据数量和稳定引用，用户确认后才通过 `PUT /v1/creator/profile` 成为新 revision。

Create 不得暗中覆盖用户输入。前端可用当前 profile 预填字段，并显示 “Using N creator preferences”；用户可逐项关闭或改写。保存的 brief 冻结具体值、profile revision 和应用字段，之后的 profile 更新不得改写旧项目。

## 6. Codex 原生适配

0.5 插件主路径只强调：

1. `memolens_status`
2. `memolens_creator_context`
3. `memolens_mixed_search`
4. `memolens_inbox_list`
5. `memolens_timeline_draft` / revise / validate

Creator context 与 Inbox 优先直接从只读 SQLite 获取，App 不运行也可使用。插件 **MUST**：

- 保持 stdio、Python 标准库、`mode=ro`、`query_only=ON`、WAL/foreign-key/busy-timeout 校验；
- 不发现或复用 Electron desktop token；
- 不直接打开 RW SQLite；
- 不暴露 save/render/export/delete 或任意路径工具；
- mixed 图片与视频结果都返回 timeline draft 所需的稳定 source/hash provenance；
- 默认输出不暴露绝对 DB/library 路径。

Codex 可建议 Inbox 决策，但 App 才能显示最终 diff 并由用户确认。未来若允许 Codex 写入，只能使用 App 签发的短期、单用途 `media.review.write` capability；不属于本阶段。

桌面 App **SHOULD** 提供一个固定的 “Continue in Codex” 入口。Electron 主进程必须自行构造指向本仓库 marketplace 的 `codex://plugins/memolens` 深链；Renderer 不得传入任意 URL，也不得因此获得通用外链打开权限。

## 7. 数据库迁移约束

现有 `V2_CHECKSUM` 对整组 v2 DDL 求 hash。实现 **MUST** 字节级冻结 v2 语句，不得向其 tuple 追加 DDL。v3 使用独立 `V3_SCHEMA_STATEMENTS` / `V3_CHECKSUM` / migration 3，并在同一事务中升级 `database_meta.schema_version=3`。

升级必须保证：database UUID、已有 assets/sources/analysis/brief/timeline 不变；失败整体回滚；重复启动幂等。

## 8. 非目标

0.5 明确不做：

- 原文件删除、移动、系统相册同步；
- 人脸身份库或人物关系推断；
- 自动发布到社交平台；
- 多创作者、云同步、CRDT；
- 自建 Agent runtime、Agents SDK 编排、Redis/Celery、独立向量库；
- 未确认的隐式个性化；
- 把检索效果写成结果型宣传或简历 bullet（需先通过 Spec 004 冻结评测）。

## 9. 发布验收

- v2 → v3、fresh v3、回滚、CAS、幂等、DB mismatch 与 token 测试全绿；
- 新素材默认 Inbox，路径 rebind 后新内容重新进入 Inbox，原状态仍归旧 asset；
- Archive 立即退出默认图片/视频检索，Undo 可恢复；
- profile edit / reset / suggestion confirm 均可解释、可版本化；
- Codex 图片 mixed result 可直接进入 Timeline draft；新工具执行时零网络、DB 字节不变；
- 桌面与 390 px：无横向溢出、操作目标至少 44×44、键盘可完成 Keep/Archive/Undo、Archive 文案明确“原文件不变”；
- 全量 Python、TypeScript、Electron、renderer、plugin validator、Skill validator、dependency audit 与 diff check 通过。

## 10. 调研来源

- 用户提供的小红书案例：[《我做的 App，两次改变了我的命运》](http://xhslink.cn/o/6E8F8TGdYew)、[《忍不住给大家剧透新功能了！》](http://xhslink.cn/o/FY1FljtLnR)。MemoLens 只吸收“回忆优先、分组复看、顺手整理”的产品原则，不复制交互或视觉。
- OpenAI 官方：[Plugins 概念](https://developers.openai.com/plugins/concepts/plugins)、[MCP server 设计](https://developers.openai.com/plugins/concepts/mcp-server)、[Build plugins](https://learn.chatgpt.com/docs/build-plugins)。这些资料支持本 Spec 的“App 掌管状态与确认，Skill + 窄 MCP 提供 Codex 能力”边界。
- 同类管理模式参考：[Google Photos 照片堆栈](https://support.google.com/photos/answer/14169846?hl=en-uk)、[Google Photos 归档](https://support.google.com/photos/answer/7362432?co=GENIE.Platform%3DAndroid&hl=en)、[Apple Photos 收藏与媒体集合](https://support.apple.com/en-us/121870)。只用于校验“分组、收藏、可恢复归档”的通用心智模型。
