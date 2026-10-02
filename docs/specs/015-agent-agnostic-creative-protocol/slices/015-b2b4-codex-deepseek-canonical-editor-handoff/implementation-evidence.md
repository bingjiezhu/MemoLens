# Implementation Evidence: ML-015-B2B4

- 最新证据快照：2026-08-29，本地 shared dirty worktree；下文保留 2026-08-23/24 的历史初验记录
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE / PROMOTION BLOCKED`
- 验证状态：`LOCAL REPOSITORY GATES PASSED; PROMOTION EVIDENCE BLOCKED`
- 发布状态：`NOT COMMITTED / NOT TAGGED / NOT RELEASED`
- Remote CI：`NOT RUN`

## Checkout identity

| Item | Observed value |
| --- | --- |
| Workspace | `<repo-root>` |
| Branch | `codex/next-generation-creator-loop` |
| Base HEAD | `0caa2cf4afcb041150c131a41c4323a907dc6b6e` |
| Current Core schema | V18 `canonical_timeline_structural_edit`；B2B4 edit/restore authority 仍来自 V11–V13，B2B4B preview action 来自 V15，B2B4C structural action 来自 V18 |
| V11 checksum | `11c5a402bbbfa70ced8477bc835fb85c3c223bec8eb200cc66e3eecfbd13a31e` |
| V12 checksum | `3cb7ae9007333c0cc65bf2f420cb31c50b2c9e2aefc4c857481805ff0722da66` |
| V13 checksum | `c28e18ceebf0c0fe5ca116917068b857db800aacff8ee05513dfeaa6c3dc6f27` |

## Implemented path

- V11 引入独立 `timeline.apply_edit` paired action 和 permanent Timeline receipt/event；V12 完成 resource-neutral `agent_project_command_receipts` convergence，旧 Blueprint 17-column receipt 以 digest-only v1 原字节迁入，Timeline exact-request 使用 v2，旧 `agent_blueprint_command_receipts` authority table 已删除。
- V13 在不修改 V12 migration/checksum 或历史 receipt/event digest 的前提下，加入独立 `timeline.restore_revision` action。Restore 以 exact current N 为 parent，把 historical K payload 仅重包为 N+1，并追加 operation/revision/head/generic v2 receipt/use event。
- Paired edit/restore routes 都绑定 exact project/database/upstream/Timeline head、one-time nonce、MLCAP1 proof 和 stable idempotency identity。actor/origin 由服务端从 capability 推导，Browser 不能声明或伪造。
- Pairing presentation 在 action set 含任一 Timeline write action 时携带 server-derived current Timeline head 与 pinned Blueprint/Coverage bindings；`timeline.apply_edit` 不隐式授予 restore。
- `memolens_canonical_editor_handoff(project_id)` 仅接受项目 ID，由插件进程重读 Blueprint/Coverage/current N/historical K/source facts，并导出无 path/secret 的 Browser projection。
- Canonical Editor 严格分离 current N、read-only historical K、pending edit 和 pending restore。Inspect 不授权写；Stage→Save 要求 exact same-binding K；edit/restore pending 互斥。
- Save 由插件进程内的 credential client 发起。成功后必须 project refresh + exact Timeline reread；response-loss/post-commit reread 不确定时保留原 pending 和同一幂等身份，不自动 rebase。
- Codex 与 DeepSeek Harness 共用同一 MCP operation、canonical editor server 和 Browser UI。
- 旧 `memolens_editor_handoff` 保留为 **Unsaved Draft Lab / Not saved / process-scoped**，不能取得 paired Timeline write 或进入 canonical ledger。
- Electron 仍负责 pairing approval/revoke 与 runtime trust；不是 Canonical Editor 的主入口。

## Focused verification observed

### Core migration, convergence and restore

- V13 Timeline restore migration/ledger：**7/7 passed**；B2B4 paired Timeline：**15/15 passed**；V12 receipt convergence：**1/1 passed**。
- B1 Core ledger：**14/14 passed**；B1a verified-prefix：**42/42 passed**；Blueprint persistence：**41/41 passed**；Timeline persistence：**10/10 passed**；Timeline edit persistence：**2/2 passed**；Creator Memory：**13/13 passed**；restore contract：**3/3 passed**。
- 上述去重后的 focused Core evidence 合计 **148/148 passed**。覆盖 fresh/populated V12→V13、future/collision/fault rollback、V12 history bytes/hash 保持、closed action、restore replay 与 tamper refusal。
- V12 convergence 已把旧 Blueprint v1 receipt 原字节收敛到 generic authority；V13 在同一 generic v2 lane 增加 restore，未重签历史 receipt/event，也未伪造 legacy request body。

### Standalone plugin parity

- Plugin receipt/restore authority：**48/48 passed**；B2A current-manifest：**2/2 passed**；Blueprint authority：**10/10 passed**；plugin entry：**16/16 passed**；pairing：**14/14 passed**。
- V13 Core/plugin rebuilt object digest parity：**17/17 objects exact**；完整 managed manifest 为 **13 migrations / 107 protected objects**，并保留显式 V12 frozen validator。
- Generic v1/v2 discriminator closure、restore receipt/event/operation linkage、orphan/cross-lane/tamper/future-schema 攻击均 fail closed。
- 历史 B2B4 初验的完整 plugin discovery：**293/293 passed in 62.869 s**；当时安装 cache 为 `0.10.1+codex.20260824043745`。当前 checkout/cache 的 successor 证据以“Latest frozen combined gate”为准，不能用这个历史版本代表现在。

### Canonical editor and renderer

- Canonical editor focused handoff/security/recovery：**16/16 passed**。
- Renderer models：**99/99 passed**。
- 已覆盖 current/historical/pending 分离、Inspect→Stage→Save restore、edit/restore 互斥、成功后 exact reread、stale/unknown-commit 保留幂等恢复身份。
- 当前最终 Node/Electron、typecheck、production build 与整仓 gate 尚未在最终 shared diff 上重跑，仍归 T055。

### Official DeepSeek Harness loader

使用 fresh isolated `DSH_HOME` 和官方 `@deepseek-ai/dsh@0.1.1-rc.2` / Node 24 运行时：

1. `plugin --profile web add <absolute MemoLens bundle path>` 成功，`plugin list` 显示 `dsh-memolens@link:.../memolens`。
2. `--profile web --dump-config` 成功解析并组合 `memolens-bundle-root`、`memolens-mcp`、`memolens-skill` 和 `memolens-prompt`。
3. 该证据验证官方 loader 的 fresh profile 安装、发现和 config composition；不等于已完成真实 DeepSeek model/UI 或双向 cross-host edit journey。

在 fresh isolated `DSH_HOME` 上另以 DeepSeek Harness `0.1.0-rc.5` source commit `47f943859bef60e4160492346772ded9b24f765a` 和 standalone Node `24.19.0` 重验：

1. `web` profile 生产 Host 成功启动；官方 browse-picker overlay 只替换不可自动化的 native picker，不修改 Harness source。
2. `/` 的 `window.__DSH_BOOT__` roster 包含 `id=dsh-memolens` 与 `/plugins/dsh-memolens/client.js`；该 client bundle 包含 **Open MemoLens Canonical Editor**。
3. `cordis.patch.yml` 必须保留 bare `dsh-memolens` / `dsh-memolens/prompt`。profile-relative文件名虽然可让 Host row 挂载，却无法进入 rc5 ClientModuleRegistry，因而会丢失 rich card。
4. Web 首次启动明确显示缺少 DeepSeek API key；未输入、读取或保存 key，也未调用真实 DeepSeek model。因此这项证据只新增真实 Web Host/UI bundle 可见性，不勾选 T053/T054。

### Persistent real-host acceptance fixture

`scripts/prepare_b2b4_real_host_fixture.py` 与 `scripts/collect_b2b4_real_host_evidence.py` 现在提供不会冒充人工验收的持久入口：

- 创建隔离 V13/WAL DB、合成 PNG Library、分离的 0700 Codex/DeepSeek state directories，以及 `codex_then_deepseek` / `deepseek_then_codex` 两个项目。
- 每个项目都有 production service 产生的 Blueprint/Coverage/Timeline revision 1 与两个可编辑 image clips。
- collector 使用 production verified ledger readers 冷读 heads/operations/desktop+paired receipts 与 FK check；输出禁止绝对路径、credential-shaped key 和秘密，并固定 `manual_acceptance.claimed=false`。
- receipt tamper、host directory permission drift、覆盖既有 fixture root 均 fail closed。
- 新 fixture 5/5 与既有双进程 harness 合跑 7/7；这仍只准备真实旅程，不替代真实 model、host、Browser 与原生手势。

2026-08-24 以该 fixture 启动 fresh Codex exec 时，插件加载已完成，但账户在模型开始前返回 usage limit（提示 2026-08-30 23:47 后重试），因此没有产生本次 fixture 上的 MCP tool call，不能记为 T053/T054 证据。此前真实 Codex tool call只证明插件/MCP可发现，当时默认 DB 不具备 canonical fixture。DeepSeek Web rc5 Host/UI bundle 已可见，但没有 API key。两项外部门都保持显式，不降级成 mock 旅程。

### Real Electron pairing presentation

2026-08-24 使用同一 fixture 启动真实 Electron 与其 managed backend，并发现/修复了一项仅在生产启动入口出现的模块身份错误：`backend/app.py` 原先把 factory 导入为 `src.*`，而 routes 使用 `backend.src.*`；同一 `AgentPairingBroker` 文件因此产生两套 Python class identity，真实 broker 会在 `isinstance` 中被误拒绝。入口现在统一导入 `backend.src`，并加入静态回归；focused setup + backend protocol 为 **15/15 passed**。

修复后，真实 CLI pairing intake 成功，Electron Library 页面显示一张 pending card，精确绑定 fixture 项目、短码 `CD9F-22C9`、`timeline.apply_edit` 与 `max_operations=1`。打开 **Review in native window** 后，原生 presentation 还精确显示 current Blueprint revision 1、canonical Timeline revision 1、Timeline/content digests、pinned Blueprint/Coverage `1/1`、900 秒窗口、到期时间和“不可授权 render/export/publication”边界。验收者选择 **Cancel**，没有点击 Allow/Reject，没有产生 capability 或写入；因此该证据证明真实 Desktop pending/review 可见性和 exact fact binding，但仍不属于 T053/T054 的跨宿主 N→N+1→N+2 model journey。

### Automated cross-host process harness

`tests/test_b2b4_cross_host_process_harness.py` 使用两个持久、分离的 Python adapter 子进程分别标记 Codex 与 DeepSeek，并在两个方向各执行 N→N+1→N+2：

- 子进程只通过 argv/env/协议取得 loopback backend URL、各自 0600 credential state 与 `project_id`；完整 stdout/stderr 和 credential tree 字节扫描未发现 main/desktop token 或 SQLite path。这证明进程/配置分离，不声称同 UID 下的 OS 安全隔离。
- 配对走真实 `AgentPairingWorkflow`，批准走 authenticated main presentation→approve，写入走真实 nonce + MLCAP1 paired Timeline route；每个 capability `max_operations=1`。
- 两个方向均验证 exact database/project/head、operation、generic receipt、capability event sequence/parent hash 连续；父测试直接扫描 host credential state，不使用 worker 自报的“未持久”字段作为证据。
- harness 包含双向 journey 与第二 worker 启动失败的资源清理 fault oracle，root 独立复跑 `PYTHONWARNINGS=error::ResourceWarning` 为 **2/2 passed in 4.277 s，零 warning**。

这只完成 T006 automated process harness；它不是 MCP/Browser UI、真实 Codex/DeepSeek model/host 或人工 journey，也不证明原生 gesture 或 OS 隔离，不能用于勾选 T053/T054。

## Latest frozen combined gate

2026-08-29 在 B2B4A/B、A0.1 与当前 shared implementation diff 上重新执行完整门禁；该证据绑定回填状态文档之前的冻结实现，而不是让文档自证自身：

- Base HEAD：`0caa2cf4afcb041150c131a41c4323a907dc6b6e`；Git-visible tree SHA-256：`425f4cb642bb91dd37dc8ea3fdae4b0678d70d4e531cb96131e695d3f7894f6c`；status snapshot SHA-256：`08d3a101f19cf994750cd743a5e77768a0c72dd162335347e93b81738233771f`。门禁前后 status stream 和 622 个 Git-visible file mode/content records 均 exact `cmp` 相同。
- `npm run check`：**exit 0**。Python unit discovery **970/970 passed in 1032.417 s**；plugin discovery **390/390 passed in 102.212 s**；Node/Electron **162/162 passed in 659.349084 ms**；renderer models **114/114 passed in 1166.684625 ms**。同时包含 Ruff、local deployment verification、renderer/Electron typecheck 与 production build。
- A0.1 production-oracle gate：inventory **167 actions / 138 exact oracles**，**138/138** exact oracle 与 offline journey 通过；这属于组合冻结树的 current count，不把 B2B4A 历史 **163/128** 快照改写成当时的数字。
- Plugin Creator validator 对 source 和 installed cache 均通过。Codex 当前安装并启用 `memolens@memolens-local` 版本 `0.10.1+codex.20260830050929`；cache 为 `<codex-cache>/memolens-local/memolens/0.10.1+codex.20260830050929`；排除 Python/cache 噪音后的 checksum rsync parity 为 **0 bytes**。
- `git diff --check` 为 **0 bytes**。证据目录：`<TEMP_EVIDENCE_ROOT>`；`npm-check.log` SHA-256 `c5e42243a34ec7194ebabbe2298348923fa5eed55aa9ce57e570d534bc1c7ea7`，`a0-verify.log` SHA-256 `434707edb9543e3419cddd490c8268924731df4407da5d6347e37c1ca0d82488`，final plugin-list JSON SHA-256 `81b250d84f587145efa8809264fc732f3a9f71ef15e990e5ceb09cf1ad223679`。
- Aggregate Python 3.14 仍输出既有 `_MediaConnection`/SQLite finalizer `ResourceWarning`；仅 playback authority/canonical editor focused lanes 已以 `PYTHONWARNINGS=error::ResourceWarning` 通过。因此只声称 aggregate gate exit 0，不声称整仓 warning-clean。

## Promotion blockers and non-claims

### Successor B2B4C current-disk addendum

[ML-015-B2B4C](../015-b2b4c-canonical-structural-edit/implementation-evidence.md) 现已在 Codex/DeepSeek 共用 Browser Canonical Editor 增加独立授权的 video Split 与 Remove from Timeline，并以 Timeline v2/V18、顶层 `structural_edit`、Stage/Discard/Save+reread 与 source-media immutability 保持 canonical 边界。这是当前 shared-worktree successor 状态，不属于上文 SHA-256 `425f4cb...` 冻结树的 B2B4A/B gate；旧 full-check 计数、plugin cache 和 source/cache parity 不能作为 B2B4C final-tree 证据。B2B4C 的 fresh reinstall 与 final-diff 全量门禁仍 pending，且它不代替本切片 T053/T054 的真实双向 model/host/UI 旅程。

- **T011 已完成**：V12 已将旧 Blueprint 17-column receipt projection/bytes/digests 原样迁入 generic v1，保持 legacy exact-request 字段为 `NULL`、保留 v1 event hash/linkage，并删除旧 authority table；V13 restore 继续复用 generic v2 exact-request lane。
- **T055 已完成**：最终整仓 gate、精确计数、diff check、plugin validation/reinstall/cache parity 均已回填；保留 aggregate ResourceWarning 边界。
- **T053/T054 未完成**：尚未用真实 fresh Codex/DeepSeek model/host/UI 完成两个方向的 N→N+1→N+2 无聊天上下文旅程并保存 exact proof。自动化双进程 harness 不能替代这项验收。
- 尚无人工 fresh Codex in-app Browser 点击和 DeepSeek UI/model 黄金旅程证据；official DeepSeek loader fresh-profile 通过只证明 loader/config path。
- 实际 Codex 安装 cache 的 fresh cachebuster reinstall 与 source 内容 parity 已验证；这不代替新任务中的真实 model/host/UI 旅程。
- 跨资源 unified history、通用 undo/redo/branch/merge、新 edit dialect、audio/subtitle/final-fidelity preview、Remote CI 与 release 未完成；Timeline 的独立 revision restore 本身已在 V13 实现。

因此该实现仍是 promotion blocked；不得把已通过的整仓 gate、自动化 process harness 或 loader profile 验证扩写成真实 host/model/UI 验收。
