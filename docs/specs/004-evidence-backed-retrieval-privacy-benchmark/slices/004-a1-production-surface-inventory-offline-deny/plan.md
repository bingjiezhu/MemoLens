# Implementation Plan: ML-004-A1 / ML-007-A1

## Invariants

1. 一份 inventory，两份父规范消费；不复制 action truth。
2. production registration 与 inventory 双向校验，manifest 不能自证完整。
3. action metadata 只保存 boundary facts，不复制领域 payload。
4. 未分类、未授权、未知、重复或缺 oracle 一律 fail closed。
5. offline 证据必须注明 instrumentation scope，不夸大为全系统 packet capture。

## Phase 1 — Closed inventory contract

- 增加 JSON schema、strict loader、canonicalizer 和 SHA-256 verifier。
- 冻结当前 Flask/editor/MCP/IPC/Bot/worker/render-export action rows。
- 验证 negative test file/case 可定位并覆盖高影响 effect。

## Phase 2 — Production registries and unknown-action gates

- Flask 从 `url_map` 动态发现。
- editor server 与 MCP 导出封闭 action/tool registries。
- Electron handler/event 通过共享 registration wrapper；禁止 raw bypass。
- Bot、worker kind 与 render/export profile 导出封闭 registry，unknown 明确失败。

## Phase 3 — Known P0 closure

- Photon 拒绝 symlink/污染路径，只发送新 re-encoded artifact，移除 original fallback。
- 对 legacy root/settings/indexing/Atlas/provider rows逐项补正确 authority/scope oracle；无法立即闭合者保持 gate RED 并拆后续任务。

## Phase 4 — Scoped offline deny

- 增加显式 `offline` network profile 与共享 send-before-deny policy。
- Python 以 audit hook 观测 DNS/connect；Node 对 net/tls/dns/fetch 加进程级 guard。
- 运行合成 Library 的搜索、索引、编辑检查、preview 与 host adapter 只读旅程，生成 path-free JSON observation。

## Phase 5 — Gate integration

- 提供一个 `verify.sh`，在同一 run 中验证 hash、surface equality、P0 oracles、Photon 和 scoped offline observations。
- 回填精确 action counts、hash、测试结果、已知 residuals；只有全部 P0 绿色才升级状态。
