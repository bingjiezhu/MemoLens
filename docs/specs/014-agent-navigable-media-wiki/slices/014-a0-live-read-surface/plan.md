# Implementation Plan: ML-014-A0

## Architecture Decision

本切片采用“实时知识投影 + 现有 canonical 读模型”，不迁移数据库。这是一个可删除的 Agent presentation layer，不是新的事实源。

```text
canonical assets / sources / explicit analysis heads
                    ↓
        private read-only SQLite snapshot
                    ↓
          MediaIndexReader + mixed search
                    ↓
             LiveMediaWiki projector
                    ↓
              MemoLensGateway
                 ↙       ↘
               CLI           MCP
```

## Files and Responsibilities

- `scripts/memolens_wiki.py`：URI parser、projection envelope、page/evidence presenter；不直接读文件或网络。
- `scripts/memolens_media_store.py`：增加只读 `segment_get`，严格复用 current-successful selector。
- `scripts/memolens_read_store.py`：增加精确 span 读 delegate，不改原 status contract。
- `scripts/memolens_core.py`：组合 Wiki projector 与现有 store/search，实现五个 Agent-neutral 方法。
- `scripts/memolens_cli.py`：增加五个 kebab-case 命令。
- `scripts/memolens_mcp.py`：增加五个严格只读工具。
- `tests/test_media_wiki.py`：独立 fixture 和合同/安全/兼容测试。

## Contract Decisions

1. `LiveMediaWiki` 只接收已压缩的结构化记录，不接收 filesystem path 或可执行内容。
2. `wiki-search` 由 Gateway 先调用现有 `mixed_search`，再投影结果；不复制排序逻辑。
3. `wiki-open` 仅解析 allowlisted URI 形状；不接收路径、SQL、URL 或网络位置。
4. Span 读取必须使用与已有 video search 同一 current-successful SQL selector。
5. 每个页面内容生成 deterministic SHA-256，它只表示本次 live page 内容，绝不命名为 generation digest。
6. 所有响应使用共同 projection/safety envelope，便于后续把 live view 替换为 pinned generation，而不破坏 Agent 主流程。

## Verification Strategy

1. 先冻结当前插件测试结果。
2. 为 URI、presenter 和 authority 分级写单元测试。
3. 使用 succeeded current + failed newer segment fixture 写数据库合同测试。
4. 进行 CLI 非仓库 cwd 与 MCP exact tool-list 测试。
5. 把五个读路径加入无网络、无写入、无路径泄漏回归。
6. 运行插件全量测试，然后运行与媒体读模型相关的项目测试。

## Deferred Follow-up

`ML-014-A1` 才会定义 V4 的 materialized generation/head/page/evidence schema、原子激活、generation pinning 和编译任务。它必须作为独立迁移，不得修改已冻结的 V2/V3 DDL checksum。
