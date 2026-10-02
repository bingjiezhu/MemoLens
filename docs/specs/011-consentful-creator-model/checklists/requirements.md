# Specification Quality Checklist: ML-011

**Purpose**：验证可同意创作者模型是否具备进入 shadow experiment 的条件

**Created**：2026-08-20

**Feature**：[spec.md](../spec.md)

## Content Quality

- [x] 避免“数字分身”式不可控人格推断，聚焦显式创作偏好
- [x] 区分确认、项目 override、task intent 和未确认 hypothesis
- [x] 需求不绑定学习算法或模型
- [x] 所有必需章节已完成

## Requirement Completeness

- [x] 没有待澄清占位符
- [x] 确认、冲突、双时间、撤销、遗忘、导出和 drift 均覆盖
- [x] Creator Context Compiler 的四类输出边界明确
- [x] Active/random/no-learning 实验与用户负担均可测
- [x] 每个用户故事有独立 Given / When / Then
- [x] 敏感推断、skip、项目例外和删除闭包 edge cases 已覆盖
- [x] 原始日志、query/model/tool trace、crash/diagnostic/support bundle 与 telemetry 均进入 never-write 或删除闭包清单
- [x] Kill criteria 与 rollback 明确
- [x] 范围、假设和依赖明确

## Feature Readiness

- [x] 未确认 hypothesis 默认应用次数要求为 0
- [x] 个性化不能覆盖 hard fact、安全和权限
- [x] 学习先 shadow，再 opt-in
- [x] 用户可以看见 counterfactual、暂停和完整遗忘
- [x] 规范明确不授权代码实现

## Review Notes

- 第 1 轮检查把长期 profile、项目 override 和当前 task 分层，并拆分 retrieval/directing/copy/render。
- 第 2 轮检查补充主动选样必须优于 random、skip 不等于负反馈和敏感类别 opt-out。
- 第 3 轮检查补充 artifact replay、删除 tombstone、drift 和无 Creator model 降级。
- 第 4 轮预注册唯一 nDCG@10 primary endpoint、active-vs-random 最小 effect/CI/strata，并定义 question/answer/skip/undo 的操作分母。
- 第 4 轮补充敏感属性 taxonomy、user-provided instruction 与 learnable preference 隔离、可审计撤销关系、最小 tombstone、备份恢复和分阶段删除 SLA。
- 第 5 轮把原始日志、trace、crash/diagnostic/support bundle 与 telemetry 纳入冻结 retention inventory，并要求删除后内容扫描，避免只删除日志索引。
- 结论：只能进入 shadow experiment；达到三次门槛前不得默认学习或应用。
