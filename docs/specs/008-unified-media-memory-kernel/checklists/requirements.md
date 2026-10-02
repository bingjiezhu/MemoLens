# Specification Quality Checklist: ML-008

**Purpose**：验证统一媒体内核规范在技术设计前是否闭合

**Created**：2026-08-20

**Feature**：[spec.md](../spec.md)

## Content Quality

- [x] 以一致性、恢复和用户可见行为为中心
- [x] 没有预先选择表结构、队列、ORM 或向量引擎
- [x] 明确采用渐进迁移而非大爆炸重写
- [x] 所有必需章节已完成

## Requirement Completeness

- [x] 没有待澄清占位符
- [x] Asset、Source、Analysis、Operation、Projection 和 Contract 均有定义
- [x] 双写、恢复、重建、library 切换和向量空间边界均可测试
- [x] 每个用户故事有独立验收场景
- [x] 写阶段故障、TOCTOU、并发 rebuild 和 migration edge cases 已覆盖
- [x] 成功标准可量化且不绑定实现技术
- [x] Rollback 和 legacy retirement 门槛明确
- [x] 范围、假设和依赖明确

## Feature Readiness

- [x] 统一内核不要求分布式微服务
- [x] 新媒体链的 revision/idempotency/source identity 被列为不可回退行为
- [x] 不同 embedding space 明确禁止直接混算
- [x] 所有 surface 通过公共 contract 解耦内部表
- [x] 规范明确不授权代码实现

## Review Notes

- 第 1 轮检查将“统一数据库”改为“统一权威语义”，避免物理合表替代领域边界。
- 第 2 轮检查补充 projection generation、并发 build 和旧 scope response。
- 第 3 轮检查补充模型空间、runtime close、public contract 和 legacy retirement 条件。
- 第 4 轮明确 Asset 是 exact-byte identity；转码/裁剪/Live Photo/近重复使用 typed relation。Review/Creator/Event correction 属于 canonical facts，只有 Inbox current view 可重建。
- 第 4 轮把 standalone audio 限定为 extension point，并冻结 projection allowlist、canonical hash、reload 容差和 14 天/10,000 operation 迁移观察分母。
- 结论：规范可进入数据模型、迁移和 operation state machine 技术计划。
