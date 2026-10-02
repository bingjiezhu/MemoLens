# Specification Quality Checklist: Script Coverage & Global Footage Assignment

**Purpose**：验证 ML-018 是否把“文稿/口播 → 私人素材 → 可编辑初剪”定义为可证伪的全片问题。

**Created**：2026-08-22
**Feature**：[spec.md](../spec.md)

## Content Quality

- [x] 用户故事以完整初剪、局部修改和可解释结果为中心。
- [x] 逻辑 Graph 不预选图数据库或求解算法。
- [x] 所有需求和成功指标可测。
- [x] 没有 `[NEEDS CLARIFICATION]` 或实施授权。

## Product Alignment

- [x] 覆盖“有文稿但配私人素材很麻烦”的核心场景。
- [x] 不把每句独立 top-k 拼接称为创新。
- [x] 结合声音、画面、文字、气口、动作和 usage。
- [x] 支持一键版本与逐步共创的同一数据链。
- [x] 尊重锁定和手工 Timeline，不全量覆盖。
- [x] 缺口先给非拍摄替代，补拍不是主流程。

## Evidence and Safety

- [x] factual、depiction、mood 和 decoration 明确分开。
- [x] 每个 clip 回链 Beat/Need/match/source span。
- [x] 硬约束违反率要求为 0。
- [x] 不完整索引、无素材、权限和能力不足有不同结果。
- [x] 用户项目反馈不自动升级为长期偏好或全局权重。

## Experiment Quality

- [x] 与 independent top-k 和 greedy baseline 公平比较。
- [x] 固定候选、分析、模型、预算和 capability。
- [x] 评估完整 preview、主动操作、事实错误、重复、边界和延迟。
- [x] 有 kill criteria、rollback 和简单查询/时间检索非劣要求。

## Pre-Planning Gate

- [ ] 维护者批准 ML-018 进入计划阶段。
- [ ] Spec 004 冻结 8–20 Beat 完整项目数据与盲评协议。
- [ ] ADR 固定 Beat/Need/Match/Assignment/Coverage Plan logical contract。
- [ ] 定义首版硬约束、软目标和不支持的 Timeline 映射。
- [ ] 建立逐句 top-1/top-k 与 greedy 可重复 baseline。
- [ ] ML-009/010 的候选 evidence 与 ML-015 operation history 已达到前置门槛。

这些门槛未完成前，只能 shadow 生成 Coverage Plan，不得替换 current Timeline 默认路径。
