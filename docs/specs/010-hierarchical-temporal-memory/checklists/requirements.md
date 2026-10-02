# Specification Quality Checklist: ML-010

**Purpose**：验证分层时间记忆规范能否进入预注册实验

**Created**：2026-08-20

**Feature**：[spec.md](../spec.md)

## Content Quality

- [x] 聚焦时间证据、事件和用户价值，没有指定视频模型或图数据库
- [x] 高层摘要与低层 evidence 的边界明确
- [x] 区分自动 hypothesis、用户确认和事实来源
- [x] 所有必需章节已完成

## Requirement Completeness

- [x] 没有待澄清占位符
- [x] 精确片段、跨媒体事件、相对时间、refinement 和增量更新均覆盖
- [x] 每个用户故事有独立 Given / When / Then
- [x] 音画冲突、时区、跨文件、短事件和绝对时间词 edge cases 已覆盖
- [x] 成功标准包含质量、压缩、成本、规模、determinism 和隐私
- [x] Kill criteria 与回退路径明确
- [x] Full graph 不是默认前提
- [x] 范围、假设和依赖明确

## Feature Readiness

- [x] 时间范围而非整段视频成为可测 evidence
- [x] Summary/episode 100% 反向引用原始 span
- [x] 正常增量路径有非平方增长的耗时与内存门槛，未预选具体索引结构
- [x] 敏感人物推断不被当作 hard identity
- [x] 规范明确不授权代码实现

## Review Notes

- 第 1 轮检查加入跨镜头/跨文件 span、音画联合证据和 coarse-vs-precise 区分。
- 第 2 轮检查加入双时间修正、绝对词限定和用户 event correction 保留。
- 第 3 轮检查加入增量/full 等价、图层 kill criteria、成本与隐私门槛。
- 第 4 轮把 primary 固定为 `R@1@IoU≥0.5`，对 mean tIoU 设置非劣门槛，并加入现有 segment、1fps、compute-matched adaptive 三个 baseline。
- 第 4 轮定义 frame/token 分母、event matching、引用正确性、1k/10k/100k p95/RSS 和 deterministic/stochastic 两条 conformance track。
- 结论：可进入时间标注集、baseline 和 projection plan 阶段。
