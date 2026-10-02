# Specification Quality Checklist: ML-009

**Purpose**：验证类型化意图与证据融合规范能否进入实验计划

**Created**：2026-08-20

**Feature**：[spec.md](../spec.md)

## Content Quality

- [x] 规范描述查询行为、证据和用户结果，没有选定模型或检索引擎
- [x] 明确 Agent 受 contract 约束，不把 Agent 等同于搜索系统
- [x] Embedding 的职责与精确约束边界清楚
- [x] 所有必需章节已完成

## Requirement Completeness

- [x] 没有待澄清占位符
- [x] 多来源、无答案、澄清、解释与预算均有要求
- [x] Source fusion paradox 有对应的失败定位和分桶指标
- [x] 每个用户故事有独立 Given / When / Then
- [x] 时间、人物、否定、source unavailable 和 mixed-language edge cases 已覆盖
- [x] 成功标准可量化且有固定 baseline
- [x] Hypothesis、kill criteria 和回退路径明确
- [x] 范围、假设和依赖明确

## Feature Readiness

- [x] Planner-only 与 retriever-only 评测可拆分
- [x] 快路径与慢路径有质量和成本门槛
- [x] 无答案不能通过填充 top-k 掩盖
- [x] 各 surface 使用同一 intent/result contract
- [x] 规范明确不授权代码实现

## Review Notes

- 第 1 轮检查将自由 Agent 调用改为受注册 clause、预算和验证约束的 planner。
- 第 2 轮检查补充 source unavailable 不得作为空集合硬交集，以及候选排除 trace。
- 第 3 轮检查加入 PhotoBench 分桶、无答案、fast/slow 路由和 kill criteria。
- 第 4 轮用同 query paired source ablation 取代跨难度 bucket 比较，增加 oracle-union/fusion loss/错误交集，并把 Event adapter 依赖移到 Spec 010。
- 第 4 轮补齐 Zero-GT precision/recall/F1、answerable coverage、selective risk、primary metric/CI、fast/slow 分离延迟和 promote/shadow/kill 三段决策，禁止靠全部拒答过关。
- 结论：规范适合预注册实验；未达到三次固定种子门槛前不得进入默认路径。
