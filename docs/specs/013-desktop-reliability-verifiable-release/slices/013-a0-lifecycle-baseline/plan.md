# Implementation Plan: ML-013-A0

## Runtime Baseline

复用 `BackendProcessSupervisor` 和现有 Node fake-child tests，验证进程所有权与 bounded lifecycle；复用 startup recovery contract 验证 SQLite job 终态。

## UI Polling Repair

`App` 为保留工作台状态而使用祖先 `hidden`，因此不通过卸载来停止副作用。`VideoWorkbench` 内部建立 visibility gate：

1. 同时检查 `document.hidden` 与根节点是否位于 `[hidden]` 祖先内。
2. 监听 `visibilitychange` 和祖先 `hidden` attribute mutation。
3. gate 关闭时 effect cleanup 取消 timeout、interval 和当前 fetch。
4. gate 恢复时依据当前 persisted job 状态重新建立 polling。

Canonical Blueprint 子工作台在 gate 关闭时卸载其只读刷新子树，重新可见后以已持久化 workspace remount，避免嵌套 authority panel 继续后台 refresh。

## Verification Boundary

本切片只验证 source-tree lifecycle。正式 app singleton、bundle/runtime inventory、签名、公证、clean VM、upgrade/rollback 属于 013-A1/B/C。
