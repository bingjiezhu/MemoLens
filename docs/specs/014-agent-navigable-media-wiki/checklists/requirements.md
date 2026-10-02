# Specification Quality Checklist: Agent-Navigable Media Wiki

**Purpose**：在进入 `plan.md` 前验证 ML-014 的完整性、可证伪性与产品对齐。

**Created**：2026-08-22
**Feature**：[spec.md](../spec.md)

## Content Quality

- [x] 用户故事以创作者结果而非数据库或模型为中心。
- [x] 明确区分功能规范、候选机制和未来技术计划。
- [x] 所有强需求均可测试，没有使用“智能”“先进”作为验收条件。
- [x] 没有 `[NEEDS CLARIFICATION]` 标记。
- [x] 父规范未授权整体实施；已授权的 ML-014-A0 由独立子规范和 checklist 管理。

## Product Alignment

- [x] 只需一个本地 Library，且全库分析不阻塞首个项目。
- [x] 原件不动；Wiki 是 projection，不是真源。
- [x] 视频证据精确到时间范围。
- [x] 人、模型、确定性过程和外部来源的 authority 分级。
- [x] Codex、Claude 与其他 Agent 通过同一 Core contract。
- [x] 项目固定 Wiki/Creator/Blueprint/Timeline revision。
- [x] 已用时间段与剩余可用时间段均可查询。

## Beyond-RAG Claims

- [x] 明确说明向量检索只是候选信号，不是事实判断。
- [x] 组合式 search/read/follow/evidence/refine 操作可单独验收。
- [x] Wiki 相对 lexical、dense RAG 和 typed hybrid 有公平 baseline。
- [x] 设定晋级门槛、停止条件和降级路径。
- [x] 没有把图数据库、Markdown 页数或模型调用数当成创新证明。

## Evidence, Privacy and Safety

- [x] 每个关键 claim 回链稳定 asset/span evidence。
- [x] 远端 Agent payload 有 manifest，whole-library upload 不在默认路径。
- [x] 页面、字幕、OCR、文件名均按不可信输入处理。
- [x] 用户确认不能被模型自动覆盖。
- [x] Projection 删除/重建不改变 canonical facts。

## Requirement Completeness

- [x] 所有用户故事都有独立测试和 Given/When/Then 场景。
- [x] Functional Requirements 覆盖层级、关系、版本、检索、usage、隐私与可移植性。
- [x] Success Criteria 有质量、效率、恢复、隐私和用户结果指标。
- [x] Edge cases、假设、依赖、回滚与非目标完整。
- [x] Google、OriNodes、开源和学术来源的适用边界已声明。

## Pre-Planning Gate

- [ ] 维护者明确批准 ML-014 进入计划阶段。
- [ ] Spec 004 固定 Wiki 复杂任务与 baseline 数据集。
- [ ] ADR 决定 canonical ledger 与 Wiki projection 的边界。
- [ ] ADR 决定 OKF-compatible export profile，而不是把 OKF 当内部数据库。
- [ ] 隐私 threat model 覆盖 Agent evidence drilldown 和 prompt injection。
- [ ] 先完成 008-A / 009-A 所需的 stable identity 与 typed retrieval slice。

未满足 Pre-Planning Gate 前，不得实施父规范的物化 generation、关系、refinement 或写回范围。已单独授权且无迁移的 ML-014-A0 不受该禁止条款限制。
