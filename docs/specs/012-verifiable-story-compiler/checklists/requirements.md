# Specification Quality Checklist: ML-012

**Purpose**：验证可验证故事编译器是否具备进入实验计划的条件

**Created**：2026-08-20

**Feature**：[spec.md](../spec.md)

## Content Quality

- [x] 区分事实、解释、用户陈述和风格文本
- [x] 明确 C2PA 验证 provenance，不证明语义真实
- [x] 内部领域模型不被 C2PA 或具体 SDK 绑定
- [x] 所有必需章节已完成

## Requirement Completeness

- [x] 没有待澄清占位符
- [x] Claim、beat、shot、revision、replay、artifact 和 export 均覆盖
- [x] 每个用户故事有独立 Given / When / Then
- [x] 虚构、绝对词、缺失 source、篡改、manifest 剥离和隐私 edge cases 已覆盖
- [x] Grounding、story、timeline、render 和 provenance 可独立评分
- [x] Success criteria 包含 coverage、correctness、replay、tamper 和 overhead
- [x] 编辑效率按 intention-to-treat 计分，未完成任务、完成率与最终错误 guardrail 均已定义
- [x] Kill criteria 与回退路径明确
- [x] 范围、假设和依赖明确

## Feature Readiness

- [x] Unsupported factual claim 不能自动 publish-ready
- [x] 旧 project replay 不读取 current profile/model 改写历史
- [x] External provenance 失败不破坏内部项目 provenance
- [x] 艺术性表达仍被允许，但不会伪装成验证事实
- [x] 规范明确不授权代码实现

## Review Notes

- 第 1 轮检查将 Story Graph 定义为编译中间表示，并拆分 claim 类型。
- 第 2 轮检查补充旧 revision replay、missing dependency 和 stage-isolated evaluation。
- 第 3 轮检查补充 C2PA hard/soft binding、AI/human disclosure、语义真实性边界和 privacy minimization。
- 第 4 轮拆分非确定 Story Planner 与确定 Story Compiler，把 98% 设为 benchmark aggregate，同时要求每个 publish-ready artifact 的 unresolved factual claim 为 0。
- 第 4 轮补充分层双人 claim-type/entailment 标注、三层非循环 hash、媒体类型 metadata budget、C2PA 分状态展示和原子发布 fault-injection。
- 第 5 轮为编辑效率加入 intention-to-treat、未完成任务成本、完成率与最终错误非劣门槛，关闭幸存者偏差。
- 结论：可进入内部 provenance model 与 evidence benchmark 计划；外部签名/密钥另立安全计划。
