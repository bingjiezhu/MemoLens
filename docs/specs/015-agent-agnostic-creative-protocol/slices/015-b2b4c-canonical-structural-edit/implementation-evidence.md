# Implementation Evidence: ML-015-B2B4C

- 证据快照：2026-08-30，shared worktree current-disk evidence
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCALLY VALIDATED / PARENT B2B4 REAL-HOST JOURNEYS PENDING`
- 发布状态：`NOT COMMITTED / NOT TAGGED / NOT RELEASED`
- Remote CI：`NOT RUN`

> 本文严格区分 current focused evidence、被后续修复超越的 earlier observation、本地 final-diff gate 与尚未运行的 promotion gate。受控测试只能证明对应代码路径，不能代替 B2B4 T053/T054 的真实 Codex/DeepSeek model/host/UI 旅程。

## Implemented path

### Core, schema and persistence

- 独立 `timeline.apply_structural_edit` authority 与 closed `split_clip | delete_clip` dialect 已实现；普通 `timeline.apply_edit` 仍只有 move/trim/set-duration/replace。
- HTTP、paired receipt 和 plugin client 的命令顶层字段是 `structural_edit`，不是 `edit`；每次只接受一个 closed object，不接受 operations array、caller Timeline、source、actor 或 origin。
- `split_clip` 只接受 video 和 absolute `source_split_ms`，左右 child 各至少 100ms，结果最多 256 clips；child IDs 由 Core 从 canonical source ranges 确定性派生。
- `delete_clip` 只移除选中 Timeline occurrence 与它的 binding，拒绝删除最后一个 visual clip；source media、evidence、Coverage 和 Blueprint 均不删除、不改写。
- Timeline schema v2 允许 split 导致的 proof-consistent repeated occurrence 与 delete 导致的 zero occurrence；v1 一一对应规则没有被放宽。ordinary edit 可在 v2 上继续工作，mixed v1/v2 history/restore 保留历史 schema。
- V18 migration 扩展 revision/operation/capability/receipt/event 闭集、Timeline v1/v2 row 与 standalone plugin mirror，同时保留 V1–V17 frozen history、backup 和 future-schema refusal。

### Service, UI and consumers

- Desktop compatibility 与 paired Agent endpoints 均调用同一 Core derivation、CAS/idempotency/receipt/replay transaction；paired path 只接受 exact `timeline.apply_structural_edit` capability。
- Codex 和 DeepSeek Harness 继续打开同一 `canonical-editor.html`、editor server 和 handoff URL。页面可见 **Split at playhead** 与 **Remove clip from Timeline**，并保留 Stage → Discard/Save → canonical reread。Pending 显示 noncanonical；unknown commit 保留 exact identity 用于 reconciliation，不自动 rebase。
- Remove 明示 `Original media remains unchanged`。Browser 不获得 pairing proof、nonce authority、desktop/main token、source path、actor/origin 或 generic write。
- structural Stage 使旧 playback lease 失效；Discard 按 current head 重签，Save+reread 按新 child/剩余 clip scope 重签。
- export actual-read admission 可接受同 evidence 的 proof-consistent repeated occurrences，仍拒绝 asset/source/proof substitution、duplicate clip ID 和错误 interval。
- React/Electron types、models、API/readers 可读取 schema v2 与 mixed history；Electron authority presentation 可显示/批准/revoke structural action。Electron 仍是 pairing/runtime authority 和 reader-compatible fallback，没有把 native Split/Remove 按钮写成本切片的主界面。

## Focused evidence observed

| Lane | Observed result | Evidence status |
| --- | --- | --- |
| Pure structural + backend/API/authority + export actual-read | **33/33 passed in 15.542s** with `PYTHONWARNINGS=error::ResourceWarning` | earlier focused observation；已被范围更大的 current 58/58 组合重跑超越 |
| V18 dedicated migration pack | **9/9 passed** with `PYTHONWARNINGS=error::ResourceWarning` | current focused evidence；包含 source same-version substitution 与 destination backup-directory swap TOCTOU fault oracle |
| Combined migration regressions | **34/34 passed** | current focused evidence；含 V17→V18/frozen predecessor/future-version lanes |
| V10 successor preservation | **1/1 passed** | current focused evidence |
| React/Electron compatibility | **29/29 + 47/47 passed**；两套 TypeScript compilation passed | current focused evidence |
| Broader plugin structural/UI/security cohort | **89/89 passed**；后加的两个 256-cap cases 又单独通过，未虚构新合并总数 | earlier focused observation |
| Post-review plugin structural Save/reread closure | **38/38 passed** | current focused evidence；对抗审查发现并修复 canonical Timeline v2 reread P1，后续又加入 fragmentless reload exact pending/save identity |
| Structural contract/backend collision, concurrency, fault, lifecycle, v2 restore and cold replay | **19/19 passed in 23.095s** with `PYTHONWARNINGS=error::ResourceWarning` | current focused evidence；三类 child-ID collision、same-head single winner、source drift zero-write、six-stage rollback、capability/transport deny、split/delete v2 K-as-N+1、historical tamper 与自洽重签 delete-target substitution |
| Structural→export/Usage vertical | exact selector **1/1 passed in 4.837s**；整个 persistence module **28/28 passed in 55.178s** with `PYTHONWARNINGS=error::ResourceWarning` | current focused evidence；真实 Split 和 Remove 驱动新 export/Usage，不是手工 duplicate fixture |
| Draft Lab process restart isolation | exact selector **1/1**；full module **11/11 passed in 5.770s** with `PYTHONWARNINGS=error::ResourceWarning` | current focused evidence；两个真实 child processes，旧进程依次 Split/Delete，新进程从原始 draft 重建；旁路的 canonical table-name sentinel SQLite exact bytes/rows 不变，不冒充真实 Core schema |
| Production structural preview barrier | exact selector **1/1 passed in 2.390s** with `PYTHONWARNINGS=error::ResourceWarning` | paired 与 desktop structural routes 在 service dispatch 前使 active old-head stream 精确失效；wrong proof 不得先撤销 |
| Structural removed-child preview vertical | exact selector **1/1 passed in 2.517s**；Safe Playback + Shared Editor cohort **6/6 passed in 10.363s** with `PYTHONWARNINGS=error::ResourceWarning` | real pairing/nonce/proof + Flask Split→Delete；旧 lease GET/HEAD expiry，新 head 对 deleted child scope denial，survivor GET/HEAD 成功；cold Timeline/bindings/ledger exact |
| Shared Browser controlled vertical | exact selector **1/1 passed** with `PYTHONWARNINGS=error::ResourceWarning` | real Core/Flask/SQLite + production client/editor backend/server + exact served `canonical-editor.html`；Stage 零写，Split Save N1→N2，Remove Save N2→N3，paired receipt/use/head 同步；不冒充真实 host/model/native gesture |
| Final-version source plugin discovery | **404/404 passed in 113.424s** with the managed repository runtime and `PYTHONWARNINGS=error::ResourceWarning` | current frozen source evidence；运行前已 bump 为 `0.10.1+codex.20260830073123`，包含 Draft Lab Split→Delete oracle |
| Production surface inventory/oracle | **170 actions / 141 exact oracles，141/141 passed，0 fail/skip**；inventory SHA-256 `c44475fefd6a97943b36afba6ce4eaa2ed96b60c9e72ab3fe282a9fd44b70d1a` | current inventory/oracle implementation evidence；Core collision closure 后已重新全量执行 |

### Root combined focused rerun

- 仓库组合：structural contract/backend + shared Browser vertical + canonical export persistence + V18 migration + production structural preview barrier，**58/58 passed in 63.741s** with `PYTHONWARNINGS=error::ResourceWarning`.
- Plugin 组合：canonical editor + Draft Lab editor，**49/49 passed in 19.727s** with `PYTHONWARNINGS=error::ResourceWarning`.
- 首次组合命令在 collection 前因把 `.agents` 文件路径写成 Python module 名而退出，没有运行任何 test；改为仓库 module 与 plugin discovery 两个正确入口后，上述两组全部通过。

### Official DeepSeek Harness loader evidence

- 官方 `@deepseek-ai/dsh@0.1.1-rc.2` 在 standalone Node **24.20.0** / pnpm **10.31.0** 与 fresh `DSH_HOME=<TEMP_EVIDENCE_ROOT>` 下，从 Codex final installed cache 执行 `plugin add`、`plugin list`、`--dump-config`，全部 exit 0。
- fresh profile 只有一个 `dsh-memolens` link；bundle-root、MCP、Skill、prompt 各 exact 一条。final cache 的 6/6 DeepSeek bundle tests 也通过；prompt/Skill/client/UI 都显式包含 `timeline.apply_structural_edit`、顶层 `structural_edit`、**Open MemoLens Canonical Editor**、**Split at playhead** 和 **Remove clip from Timeline**。
- 未读取或输入 DeepSeek API key，未运行真实 model/UI edit journey。这只验证 official loader/config composition，不改写 T053/T054。

### Superseded observations

- Full plugin discovery 曾在 P1 修复前观察到 **402/402 passed**，后续又观察到 **403/403** 与 pre-final **404/404 in 160.125s**。这些数字均已被 final-version **404/404 in 113.424s** 超越。
- 一次直接使用系统 Python 3.14 的错误 discovery 只收集 **305** 个并因缺 Flask 产生 4 个 import error。同一 source 紧接着用仓库 `scripts/run_python.sh` 受管运行器完整重跑为 **404/404**；失败根因是运行器依赖不完整，未据此修改产品代码。

### Final local repository gates

- 第一轮 final-diff `npm run check` 在 Python discovery 中运行 1003 项并发现两个过时 V18 测试预期：`test_b2a_no_migration` 与 `test_video_media` 仍把 current migration lineage 截止到 V17。只更新这两个 oracle 后，精确重跑 **2/2 passed in 0.868s** with `PYTHONWARNINGS=error::ResourceWarning`。
- 第二轮从头执行 `npm run check`，最终 exit 0。观察到 Python **1003/1003**、standalone plugin **404/404**、Node/renderer-model **120/120**；local verify、TypeScript/Vite/Electron build 也因命令链完整到末端而通过。第二轮 Python runtime 未被持久捕获，不能事后补造。
- full `npm run check` 没有把 Python `ResourceWarning` 升格为错误，且旧测试路径仍输出 unclosed-database warnings；因此只声明全仓 exit 0。B2B4C focused **58/58 + 49/49** 继续是独立的 warning-as-error 证据。Node 仅观察到 experimental loader/type-stripping toolchain warnings，不是产品测试失败。
- tracked `git diff --check` exit 0。首次对 **270** 个 untracked 文件逐一执行 no-index whitespace check 时发现 10 个 EOF 多余空行；机械删除后再次全量扫描，exit 0。普通 `git diff --check` 不被冒充为覆盖 untracked 文件。
- final source↔installed plugin cache `rsync --checksum --delete` 再验证为零输出、exit 0。

## Repository gates

| Gate | Observed result | Status |
| --- | --- | --- |
| Focused structural/backend/export | 33/33 | EARLIER PASS; SUPERSEDED BY CURRENT 58/58 |
| Focused V18/migration/V10 | 9/9 + 34/34 + 1/1 | PASS |
| Focused React/Electron/typecheck | 29/29 + 47/47 + two TypeScript compilations | PASS |
| Focused plugin structural Save/reread after P1 fix | 38/38 | PASS |
| Structural contract/backend collision/concurrency/fault/lifecycle/v2 restore/cold replay | 19/19 | PASS |
| Structural export/Usage vertical | 28/28 | PASS |
| Draft Lab Split→Delete restart/no-canonical-mutation | 11/11 module；exact restart selector included | PASS |
| Structural removed-child preview vertical | 1/1；related cohort 6/6 | PASS |
| Shared Browser controlled N1→N2→N3 vertical | 1/1 | PASS |
| Root combined focused rerun | 58/58 + 49/49 | PASS |
| Production surface oracle/inventory | 170 actions / 141 oracles；141/141 | PASS |
| Final-version source plugin discovery | 404/404 in 113.424s | PASS |
| Plugin manifest validator | Final source and final installed cache both passed | PASS |
| Fresh plugin reinstall/source↔installed-cache parity | Codex installed/enabled `0.10.1+codex.20260830073123`；`rsync --checksum --delete` zero output；cache editor 49/49 + DeepSeek 6/6 | PASS |
| Official DeepSeek loader fresh-profile revalidation | rc.2 / Node 24 add+list+dump from final installed cache；all exit 0，all five bundle rows exact-one | PASS |
| Independent implementation review | P0/P1 = 0 | PASS |
| Full Python/plugin/Node/renderer/typecheck/build | Python 1003/1003；plugin 404/404；Node/renderer 120/120；local verify and builds reached | PASS；second-run Python runtime not captured |
| `npm run check` | First run exposed two stale V18 oracles；after focused 2/2 repair, second full run exit 0 | PASS |
| Final tracked + untracked whitespace | tracked `git diff --check` exit 0；270 untracked no-index checks exit 0 after 10 EOF-only fixes | PASS |
| Remote CI | Not run | NOT VALIDATED |

## Real-host promotion gates

- B2B4 T053 fresh Codex→DeepSeek host/model/UI journey：`NOT RUN / INDEPENDENT BLOCKER`
- B2B4 T054 fresh DeepSeek→Codex host/model/UI journey：`NOT RUN / INDEPENDENT BLOCKER`

Focused UI tests、adapter-process harness、production inventory、in-app Browser inspection 或 screenshot 都不能改写上述状态。只有原 B2B4 gate 定义的 fresh 真实 model/host/UI 双向旅程能闭合 T053/T054。

## Residuals and non-claims

- Codex 已安装并启用 `0.10.1+codex.20260830073123`，final source 与 installed cache checksum parity 为零差异；但当前任务在 reinstall 前已启动，因此不声称本任务已 hot-load 新 Skill/MCP，也不冒充 fresh Codex model/UI journey。
- 不声称 full repository gate warning-free；它 exit 0，但普通全仓运行仍暴露旧 unclosed-database `ResourceWarning` 卫生债，第二轮 Python runtime 也未捕获。
- 不声称 Electron 已有与 Browser 同等的 Split/Remove 按钮；当前完整可见结构编辑面是 Codex/DeepSeek 共用 Browser Canonical Editor。
- 不声称 Remove 会删除 source media；它只移除 canonical Timeline occurrence。
- 不声称 Unsaved Draft Lab 获得 canonical Save；它的宽方言 split/delete/fit/volume/canvas/undo/redo 仍是 process-scoped。
- 不声称 image/audio split、transition、crop、speed、multi-track、source audio/BGM/voiceover/mixing、subtitle 或 final-fidelity render/export 已交付。
- T046 现已有单体 real Split→Delete→removed-child old/new lease GET/HEAD vertical oracle；它仍是 controlled-local route evidence，不冒充真实 host gesture 或 final-fidelity playback。
- 不声称 full Remote CI、commit、tag、release 或真实 host promotion 已完成。
