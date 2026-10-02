# Implementation Plan: ML-015-A0

## Architecture Decision

本切片采用“单 SQLite 快照 + 严格只读投影”。现有 project/brief/timeline 仍是真源；Resume Capsule 只为 Agent 压缩当前上下文，不写回也不缓存。

```text
creative_projects + creative_briefs + timelines
                       ↓
            private read-only snapshot
                       ↓
              ProjectIndexReader
                       ↓
             ProjectResumePresenter
                       ↓
                MemoLensGateway
                   ↙       ↘
                 CLI           MCP
```

## Compatibility Matrix

| 关系 | 本切片最低合同 | 缺失时行为 |
| --- | --- | --- |
| `creative_projects` | `id,title,status,created_at,updated_at` | project read 整体 unavailable |
| `creative_briefs` | `project_id,revision,brief_json,content_sha256,provenance_json,created_at` | 项目可列出，open 返回 incomplete，不伪造 capsule |
| `timelines` | 沿用 `TimelineIndexReader` 的 9 列基线 | 项目仍可打开，Timeline/history gap |
| `timeline_revisions` | 同上，ID 列为 `timeline_id` | 仅作兼容 adapter；canonical `timelines` 优先 |
| `database_meta` | `database_uuid,schema_version` 可选 | 响应增加 identity gap，不阻塞读取 |

`creative_briefs` 只加入固定 SQLite relation allowlist；这不是 migration。

## Reader and Presenter Boundaries

- `ProjectIndexReader` 拥有一次 snapshot，在同一 connection 中读 project、brief 和 Timeline。
- `ProjectIndexReader` 复用 `TimelineIndexReader.schema(connection)` 的 canonical/compatibility 固定优先级，然后在自己的同一 connection 中查询；不修改现有 Timeline `list/get` 语义。
- `ProjectResumePresenter` 仅处理字段白名单、不可信标记、digest 校验、evidence URI 与 gap；不直接查 DB。
- CLI/MCP 只是 Gateway 薄适配层，不重复选 head 或泄漏过滤逻辑。

## Head and Integrity Rules

1. Brief head 是指定 project 内数字最高 revision，但仍只称 legacy projection。
2. Timeline 先求每个 logical timeline ID 的 `MAX(revision)`，再在 branch heads 中按 `created_at DESC, revision DESC, ID ASC` 选择。
3. 不使用 `creative_projects.updated_at` 选 head，因为现有写路径不保证修订 Timeline 时更新它。
4. 不过滤 invalid latest revision，不回退到较旧 valid revision。
5. 对 stored JSON 原字符串计算 SHA-256；仅在 digest 匹配且 JSON 是 object 时生成内容摘要/evidence。
6. `parent_revision`、`brief_revision` 或 `validation_errors_json` 列不存在时返回能力 gap，不从 provenance 自由文本猜测。

## Contract Decisions

1. `project-list` 使用 project ID keyset，缓解稳定性问题；项目 title 只是 untrusted display data。
2. Resume 不携带完整 Timeline，Agent 必须按 `next_actions` 显式读取 `timeline-get`。
3. Brief list/text 字段都有深度、项数和长度上限；所有未知 key 丢弃。
4. Operation summary 只允许固定 operation type 和符合 ID 规则的 subject；intent/reason/value/precondition/raw actor 不返回。
5. Evidence ref 只是稳定标识；它不声明 source 仍可用，Agent 使用 `wiki-evidence` 获取当前可解引性。
6. `project-list` 只验证项目行与修订行是否存在，因此用 `presence_only`而非 `complete`；深度完整性在 `project-open` 中验证。
7. Timeline 派生内容使用 integrity + validation + brief-binding 联合门槛；失败时只返回行 identity、integrity、validation 和 gaps。

## Files and Responsibilities

- `scripts/memolens_project_store.py`：单快照 project/brief/timeline/history reader。
- `scripts/memolens_project_resume.py`：白名单投影、完整性、evidence 和 gap presenter。
- `scripts/memolens_text_safety.py`：Resume/Wiki 共享的 Unicode 边界 locator 检测，防止脱敏漂移。
- `scripts/memolens_sqlite.py`：将固定 `creative_briefs` 加入 relation allowlist。
- `scripts/memolens_read_store.py` / `memolens_core.py`：组合项目读模型并暴露 Gateway。
- `scripts/memolens_cli.py` / `memolens_mcp.py`：三个中性读入口。
- `tests/test_project_resume.py`：canonical/legacy/corrupt/multi-branch/隐私/无副作用合同。

## Verification Strategy

1. 先记录当前 plugin 基线，不修改旧 fixture 意义。
2. 使用 `proj_main` / `proj_archived` / `proj_orphan` 和两个 Timeline branch 的专用 canonical fixture。
3. 从 fixture 派生 digest mismatch、JSON 损坏、缺 project/brief、`timeline_revisions` fallback 和双关系优先级变体。
4. 验证三个 Gateway/CLI/MCP 合同和实际 output schema。
5. 监控 snapshot 创建次数，并禁止 DNS/socket/原媒体打开/SQLite 写。
6. 增加 invalid validation、dangling/stale brief binding、locator 边界/空格、非整数 MCP limit 和 CLI parser error 对抗测试。
7. 运行 plugin 全量、项目 unit/node/typecheck、Ruff、compile 和 diff check。

## Deferred Follow-up

- `ML-015-A1`：定义真正 Creative Blueprint candidate/schema/validator，仍先 shadow/read-only。
- `ML-015-B`：统一 typed writes、CAS/idempotency、显式 project head 和完整 operation history。
- 完整 Open Project package、App 恢复 UI、Wiki generation pinning 和跨环境 migration 均需独立规范与授权。
