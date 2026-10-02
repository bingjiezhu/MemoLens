# Feature Specification: Desktop Lifecycle Baseline

- Feature ID：`ML-013-A0`
- 状态：`IMPLEMENTED / LOCALLY VALIDATED`
- 父规范：[ML-013](../../spec.md)
- 优先级：Immediate gate

## Objective

冻结当前源码桌面运行时已具备的后端身份、单 owner startup、bounded retry、stop/kill、job interruption recovery 与 UI polling lifecycle；同时阻止这些本地测试被误写成正式安装包、签名、公证或升级验证。

## Current Truth

- backend 以单进程、无 reloader 方式启动；Electron supervisor 合并并发 ensure、限制重试、旋转 credential，并在无法确认旧 writer 退出时拒绝拉起新 writer。
- `before-quit` 等待受管 backend 停止；后端激活时把遗留 running jobs 转为 interrupted。
- 当前尚未调用 Electron `app.requestSingleInstanceLock()`，因此“整个桌面应用只允许一个实例”仍未闭合。
- 当前仓库从源码和 `.venv` 启动，不是已签名、公证、带 sidecar runtime/FFmpeg 的 clean-machine release artifact。
- `VideoWorkbench` 会被 `hidden` 祖先保留挂载；重复轮询必须在工作台不可见或 document hidden 时停止，并在重新可见时恢复。

## Requirements

- **FR-A0-001**：必须存在单一入口 `bash docs/specs/013-desktop-reliability-verifiable-release/slices/013-a0-lifecycle-baseline/verify.sh`。
- **FR-A0-002**：入口必须覆盖 backend health identity、并发 startup 合并、bounded retry、stop escalation、旧 writer 未退出时拒绝 replacement、index coordinator cleanup 与 interrupted job recovery。
- **FR-A0-003**：隐藏或后台 Video Workbench 不得继续 Blueprint、index-job 或 render-job 周期轮询；重新可见时允许从持久状态继续。
- **FR-A0-004**：本地 source-tree pass 不得标为 clean install、single desktop instance、signed/notarized、N-1 migration 或 GA ready。
- **FR-A0-005**：缺少真实 release artifact 时相关验收保持 `not_run`，不得用 mock supervisor 代替。

## Success Criteria

- lifecycle 入口整体退出码为 0。
- 隐藏/后台轮询门闩有直接回归测试并通过 TypeScript 编译。
- 文档与测试均保留 single-instance、clean bundle、签名、公证、升级/回滚缺口。

## Local Validation Evidence

2026-08-23 使用隔离的 Python 3.12.13 / SQLite 3.53.1 运行 `verify.sh`：35 项 Electron supervisor/index coordinator/polling、1 项 startup recovery、12 项 SQLite runtime，共 48 项通过；Electron build 与双 TypeScript typecheck 通过。该结果只覆盖 source-tree lifecycle，未运行 clean-machine、签名、公证、desktop singleton 或升级/回滚矩阵。
