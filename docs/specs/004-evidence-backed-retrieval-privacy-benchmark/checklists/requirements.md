# Specification Quality Checklist: ML-004

**Purpose**：在进入技术计划前验证 Spec 004 的完整性与可测试性

**Created**：2026-08-20

**Feature**：[spec.md](../spec.md)

## Content Quality

- [x] 没有预先绑定语言、框架、数据库或模型
- [x] 以用户价值、发布治理和可验证结果为中心
- [x] 非实现人员可理解核心目标
- [x] 必需章节已完成

## Requirement Completeness

- [x] 没有待澄清占位符
- [x] 功能要求可测试且编号稳定
- [x] 成功标准可量化
- [x] 成功标准不依赖具体实现技术
- [x] 所有用户故事都有 Given / When / Then 验收
- [x] 已列出边界与异常情况
- [x] 范围与不做事项明确
- [x] 依赖和假设明确

## Feature Readiness

- [x] 用户故事可以独立验证
- [x] 质量、隐私、性能和声明治理均有门槛
- [x] 外部 benchmark 的许可证与外推风险已声明
- [x] 现状 baseline 不因预期结果而被选择性省略
- [x] 规范明确不授权代码实现

## Review Notes

- 第 1 轮检查发现旧 Spec 005/006 指向不存在的 004；本规范已补齐路径，但不追认旧结果声明。
- 第 2 轮检查补充了无答案、多来源分桶、删除闭包、incomplete run 和外部许可证边界。
- 第 3 轮检查确认每项后续创新都能引用统一 baseline，并有失败时不进入 release 的规则。
- 第 4 轮检查补充 production action inventory hash、deletion-closure policy matrix、失败抽样分母，以及 primary metric/CI/容差的强制预注册字段。
- 结论：规范可进入单独的技术规划；本轮保持 `PROPOSED / NONE`。
