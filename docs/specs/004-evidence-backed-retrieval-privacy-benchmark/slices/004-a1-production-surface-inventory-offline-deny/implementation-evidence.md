# Implementation Evidence: ML-004-A1 / ML-007-A1

- 验证日期：2026-08-29
- 状态：`IMPLEMENTED / LOCALLY VALIDATED (SCOPED PROCESS OFFLINE GATE)`
- 可重放入口：`bash docs/specs/004-evidence-backed-retrieval-privacy-benchmark/slices/004-a1-production-surface-inventory-offline-deny/verify.sh`
- 验证对象：当前工作树中的 production registrations、canonical inventory、exact negative oracles 与 offline policies

## Frozen Inventory Snapshot

同一次 `verify.sh` 运行完成 production discovery、双向集合比较、closed-schema/self-hash 检查和 exact oracle 执行，最终快照为：

| 项目 | 结果 |
|---|---:|
| Production actions | 159 |
| Exact negative oracles | 121 |
| Exact oracle run | 121/121 passed |
| High-impact authority/scope gaps | 0 |
| Inventory SHA-256 | `113315945a985c2aa8ceafaeb9dbd9cf619e18b9b804a9536428347d67903395` |

按 surface 的动态发现计数：

| Surface | Actions |
|---|---:|
| `electron_ipc` | 16 |
| `flask_http` | 86 |
| `mcp_tool` | 29 |
| `photon_bot` | 6 |
| `plugin_editor_action` | 13 |
| `plugin_editor_http` | 5 |
| `python_worker` | 2 |
| `render_profile` | 2 |

这些数字是当前 inventory hash 所固定的验证快照；生产注册源、action、oracle selector 或 mapping 任一变化，都要求重新运行 gate，不能继续沿用本页结果。

## Same-run Verification Results

单一入口在同一次成功运行中完成：

- renderer build：通过；
- Electron build：通过；
- Photon TypeScript typecheck：通过；
- hash-bound exact negative oracles：121/121 通过；
- setup offline policy：6/6 通过；
- instrumented offline production journey：1/1 通过；
- Atlas read-purity：7/7 通过；
- DeepSeek Harness plugin contracts：6/6 通过；
- Node local API / Timeline preview contracts：10/10 通过；
- 最终 inventory loader/self-hash、surface census 与 `high_impact_oracle_gaps` 检查：通过。

该入口以非零退出表示任一 build、typecheck、selector、offline journey、hash、census 或 gap 检查失败；因此上述分项属于同一 gate，而不是从多次不一致运行拼接出的绿灯。

## Offline Observation Boundary

本次 offline journey 对受测 Python/Node/Electron 进程中的 provider、DNS、socket、fetch/send 边界进行发送前拦截，并只保存不含 credential、payload、私人媒体和绝对 Library/DB path 的观察。允许 literal loopback / Unix socket；受 instrumentation 覆盖的非 loopback DNS/connect/fetch/send 观察计数为 0。

这项证据严格限定为 scoped process instrumentation：

- 不等同于 OS packet capture，也不声称观察了机器上所有进程；
- 不证明同 UID 恶意进程隔离；
- 不证明 clean-machine packaging、签名、公证或正式 release；
- 不证明真实远端 Codex/DeepSeek host UI、原生手势或 model/provider journey；
- DeepSeek Harness 的 6 项结果是本地 plugin contract 验证，不是一次真实远端模型调用。

因此，A1 可以晋级为 `LOCALLY VALIDATED`，但这些非目标仍由各自的 release、host/model journey 与 B2B4 promotion gate 持有，不能从本切片外推。
