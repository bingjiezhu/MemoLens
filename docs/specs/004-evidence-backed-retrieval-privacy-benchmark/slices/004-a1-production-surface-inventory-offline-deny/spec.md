# Feature Specification: Production Surface Inventory & Offline Deny Gate

- Feature IDs：`ML-004-A1` / `ML-007-A1`
- 状态：`IMPLEMENTED / LOCALLY VALIDATED (SCOPED PROCESS OFFLINE GATE)`
- 父规范：[ML-004](../../spec.md) / [ML-007](../../../007-local-capability-boundary/spec.md)
- 优先级：V0 P0 gate

## Objective

建立一份由 ML-004 与 ML-007 共用的 production action 真源，并以生产实现反向发现当前 surface：

```text
production HTTP / editor HTTP / Electron IPC / MCP / Bot / worker / render-export
  ↔ one closed action inventory
  → deterministic inventory hash
  → authority + scope + negative oracle
  → offline non-loopback deny observations
```

这份 inventory 只描述“哪些生产动作存在、各自需要什么边界和证据”，不复制领域 payload，不签发 capability，也不成为第二份项目真源。

## Current Truth

- 当前受控本地 inventory 包含 159 个 production actions：Electron 16、Flask 86、MCP 29、Photon 6、editor action 13、editor HTTP 5、Python worker 2、render profile 2。精确数量仍由 gate 动态发现，本文数字只是本次验证快照，不替代 machine-readable inventory。
- 159 个 action 均绑定至少一个可定位的 exact negative oracle；去重后共 121 个 oracle，且高影响 authority/scope gap 为 0。
- Canonical Editor / Unsaved Draft Lab 的 loopback editor HTTP 与页面 action、Electron legacy indexing、Python worker、durable media/video、preview render、canonical export 与 Discord Photon 均已进入同一 inventory，不再只扫描 Flask。
- inventory canonical SHA-256 为 `113315945a985c2aa8ceafaeb9dbd9cf619e18b9b804a9536428347d67903395`；loader 会重算并拒绝 hash、schema、排序或 action/oracle mapping 漂移。
- `offline` production profile 已在受测 Python/Node/Electron 进程的 provider、DNS、socket、fetch/send 边界执行发送前拒绝；同次本地 journey 中非 loopback 观察计数为 0。
- 该 offline 结论是 scoped process instrumentation，不是 OS packet capture，也不证明同 UID 恶意进程隔离、clean-machine release 或真实远端 host/model journey。

## User Stories

### US1：维护者新增生产动作时立即得到红灯（P0）

**Given** 维护者新增 HTTP route、IPC channel、MCP tool、Bot action、worker kind 或 render/export profile，**When** 运行 A1 gate，**Then** 未声明 action、重复 ID、未知 effect、缺少 authority/scope 或缺少负向 oracle 都必须失败。

### US2：发布证据能固定“测试过的正是这组动作”（P0）

**Given** 一个可比较的发布候选，**When** 生成 action inventory hash，**Then** canonical bytes、schema version、action set 和 test mapping 均可重放；inventory 变化而相应测试映射未变化时，该 run 无效。

### US3：离线模式不靠“刚好没 key”维持隐私（P0）

**Given** `offline` production profile，**When** 运行搜索、索引、剪辑检查、preview 与双宿主只读主链，**Then** literal loopback / Unix socket 以外的 DNS、socket、fetch/provider send 在发送前被拒绝并形成不含路径或 payload 的观察记录。

### US4：Photon 只发送安全派生物（P0）

**Given** 越界 symlink、污染路径、损坏或伪装图片、重编码失败或尺寸仍超限，**When** Photon 准备 Discord attachment，**Then** 不发送原路径、原文件或原 bytes；动作明确失败或跳过。

## Requirements

- **FR-A1-001**：仓库必须只有一份 versioned machine-readable production surface inventory；ML-004 与 ML-007 共同消费它。
- **FR-A1-002**：inventory schema 必须 closed，拒绝未知字段、重复 action、非有限数字、未排序/重复集合和未知 enum。
- **FR-A1-003**：每个 action 必须包含稳定 ID、surface、owner、effects、authority principal/requirements、resource scope/bindings、network class、contract version 和至少一个可解析的 negative test oracle。`scope` 必须命名真实的资源绑定维度；`GET:/...`、`POST:/...`、`ipc:...`、`tool:...`、Bot action、worker kind 或 render profile 名称都只是 action identity，不是 resource scope。
- **FR-A1-004**：effects 至少区分 `read`、`mutation`、`filesystem-read`、`filesystem-write`、`process-start`、`egress`；一个 action 可有明确组合，不能用模糊 `other`。
- **FR-A1-005**：gate 必须从生产注册源发现 Flask、editor HTTP/action、Electron invoke/event、MCP、Bot、worker 和 render/export action，并与 inventory 做双向集合相等比较。
- **FR-A1-006**：生产注册边界必须拒绝 unknown/duplicate action；不得把 manifest 自己当作唯一“发现源”。
- **FR-A1-007**：inventory hash 必须对排除 `inventory_sha256` 字段后的 canonical JSON 计算 SHA-256；文件内 hash 与重算值必须一致。
- **FR-A1-008**：每个 mutation/filesystem/process/egress action 的 oracle 必须验证对应 authority 与 scope 的拒绝行为；仅引用 census test、registry equality 或 closed request-shape test 不足以标绿。
- **FR-A1-009**：Photon attachment 必须来自重新 decode/re-encode 的新工件；任一步失败必须 fail closed，不得 fallback original。
- **FR-A1-010**：render/export profile 与 worker kind 必须使用封闭 registry；任意字符串不得静默进入 export 尺寸或被 worker 忽略。
- **FR-A1-011**：offline policy 必须在 provider/DNS/socket/fetch 发送前拒绝非 loopback 目标，不能依赖空 API key 或测试不触发分支。
- **FR-A1-012**：offline gate 必须记录监测 scope。只 instrument Python/Node 进程时，不得声称完成 OS 全进程 packet capture。
- **FR-A1-013**：观察和证据不得保存 token、credential、原始媒体、私人 payload、绝对 Library/DB path 或可逆 provider body。
- **FR-A1-014**：已发现但尚无正确 authority/oracle 的 action 必须使 gate 保持 RED；不得用 waiver 把 P0 标绿。

## Success Criteria

- production action discovery 与 inventory 的双向差集均为空。
- 100% actions 具有 closed classification、authority/scope 和至少一个存在且可定位的 negative oracle。
- 注入任意一个 fake route/tool/channel/job/profile/action 时 gate 非零退出。
- inventory canonical hash 重算完全一致；内容变化而 oracle mapping 不变时 run 被判无效。
- offline instrumented production journeys 的非 loopback DNS/connect/fetch/send 计数为 0。
- Photon symlink、损坏、重编码失败和 oversize fixtures 中原件被发送次数为 0。
- 全部 P0 negative tests 通过且无 waiver 后，切片才可从 RED 升级为 locally validated。

## Explicit Non-goals

- 本切片不建立通用 policy DSL、全产品 token service 或完整 provider grant 平台。
- 本切片不声称 OS 级恶意同 UID 进程隔离。
- 本切片不实现完整公开 benchmark、deletion closure、clean-machine release 或真实远端 provider journey。
- 本切片不替代 B2B4 的 fresh Codex↔DeepSeek model/UI/原生手势验收。

## Promotion Rule

实现 inventory 文件但仍存在未覆盖 Bot、editor HTTP、worker、render/export 或已知 P0 authority/egress 缺口时，状态只能是 `IMPLEMENTED / GATE RED`。只有 discovery、hash、negative-oracle 与 scoped offline gate 同次通过，才能写 `LOCALLY VALIDATED`。

当前切片已按该规则完成同次本地验证；可重放命令、分项计数与声明边界见 [implementation-evidence.md](./implementation-evidence.md)。任何 inventory、生产注册源、oracle selector 或 offline policy 变化都会使既有快照失效，必须重新运行 gate。
