# Specification Quality Checklist: Agent-Agnostic Creative Protocol & Open Project Chain

**Purpose**：验证 ML-015 在进入计划前是否完整、可测且与 43 问决策一致。

**Created**：2026-08-22
**Feature**：[spec.md](../spec.md)

## Content Quality

- [x] 以用户旅程和可观察结果为主。
- [x] 未选择具体 CLI 库、IPC 框架、文件布局或进程拓扑。
- [x] 所有需求可验证且没有 `[NEEDS CLARIFICATION]`。
- [x] 明确已授权/已实施切片 A0–B2B4C 与尚未完成的 remainder/B2C/Open Project 分开管理；本 checklist 最初创建于 2026-08-22，后续状态以各 slice evidence 为准。

## Decision Alignment

- [x] 不内置第二套聊天或单独模型 key。
- [x] Codex、DeepSeek Harness 与未来 Agent 使用同一 Core 和 shared Canonical Editor business contract。
- [x] 对话编译为 Creative Blueprint，不以聊天为真源。
- [x] 直接 first cut 与逐步共创是一条连续流程。
- [x] 对话和 UI 操作共用 action-level history。
- [x] 项目包开放、可读、可迁移、默认不复制原件。
- [x] Library 冷启动只有一个概念性步骤。
- [x] Browser Canonical Editor 是主剪辑面；Electron 只保留 native authority/runtime/broker 与兼容 reader 职责。

## Requirement Completeness

- [x] 用户故事覆盖冷启动、first cut、历史、跨 Agent、权限和项目迁移。
- [x] 写命令具备 idempotency、CAS、typed diff 和统一 handler 要求。
- [x] 明确 Agent capability discovery 与语义写回来源。
- [x] Success Criteria 同时覆盖用户时间、跨 Agent 一致性、重放、迁移和安全。
- [x] Edge cases、回滚、假设、依赖和非目标完整。

## Remainder / B2C / Open Project Pre-Planning Gate

- [ ] 用户/维护者批准 ML-015 remainder/B2C/Open Project 的具体实施切片。
- [x] 首批 host adapter 目标已收敛为 Codex plugin 和 DeepSeek Harness bundle，两者复用同一 MCP/skill/plugin code。
- [ ] 通过首批 Codex/DeepSeek 真实 model-call 黄金旅程并固定最小多模态支持矩阵。
- [ ] ADR 固定 Core command / capability / effect class 模型。
- [ ] ADR 固定 Open Project logical schema 与 migration policy。
- [ ] Spec 004 增加跨 Agent 黄金旅程和 operation replay fixture。
- [ ] 完成现有 plugin read-only contract 与拟议 Core command 的差异报告。

## Current Browser/editor promotion boundary

- [x] Fresh Codex cachebuster install、source↔cache parity 和 installed-cache subset 通过。
- [x] Official DeepSeek Harness fresh profile 装载 shared bundle 并启动 MCP child。
- [x] Controlled-local Codex Browser 完成 AAC transport 视觉播放，且 canonical ledgers 不变。
- [ ] DeepSeek Harness 在 shared Canonical Editor 中完成同一 AAC Browser journey。
- [ ] Codex 和 DeepSeek 各自完成 fresh real model tool call 及双向 host-model-UI T053/T054。
- [ ] 真实用户/native audio-device 验收观察实际扬声器静音。
- [ ] Remote CI、clean-machine acceptance 与 release 通过。

这些未完成门槛只约束 remainder/B2C/Open Project、real-host promotion 与更广的项目格式；A0–B2B4C 由各自 slice spec、实施授权和验收证据管理。B1 的 proposal commit/restore、B2B4 的 Timeline edit、B2B4C 的 structural edit 和 B2C0 的 restore 是四类分别授权、分别校验、分别留据的 typed capability；它们不组合成 generic write，也不产生 semantic confirmation。
