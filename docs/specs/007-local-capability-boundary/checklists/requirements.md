# Specification Quality Checklist: ML-007

**Purpose**：验证本地能力边界规范在实现前是否完整

**Created**：2026-08-20

**Feature**：[spec.md](../spec.md)

## Content Quality

- [x] 没有把风险夸大为无条件远程 RCE 或本地提权
- [x] 区分正常用户功能、受损 surface 和本机同用户威胁
- [x] 需求描述结果，不绑定 token、签名或 policy 实现
- [x] 所有必需章节已完成

## Requirement Completeness

- [x] 没有待澄清占位符
- [x] 命令启动、文件读取、mutation、egress 和撤销均有要求
- [x] Renderer、backend、Photon 与 provider 边界均被覆盖
- [x] Given / When / Then 场景可独立测试
- [x] Symlink、TOCTOU、Origin、token 和 grant 边界已列出
- [x] 成功标准可量化且技术无关
- [x] 降级行为明确 fail closed
- [x] 范围、假设和依赖明确

## Feature Readiness

- [x] 所有 P0 negative tests 要求 100% 通过
- [x] 正常用户旅程有非回归标准
- [x] Capability 无法被 raw path/settings 隐式扩大
- [x] 远端发送有可核对 disclosure 目标
- [x] 规范明确不授权代码实现

## Review Notes

- 第 1 轮检查将 Python command 风险限定为 renderer compromise 后的能力跨界，不描述成 shell injection。
- 第 2 轮检查将 Photon 风险限定为 library symlink/污染路径加 allowlisted 用户触发，并加入原文件 fallback 的 fail-closed 门槛。
- 第 3 轮检查补充了 backend settings 重授权、originless、撤销和实际 payload 对账。
- 第 4 轮检查补充可信 policy owner、main-owned native confirmation、nonce/operation digest、恶意 renderer 签发/伪造/重放测试、首次 root discovery/source manifest，以及 provider grant 的 pre-network/ambiguous 消费语义。
- 第 4 轮同时把普通 domain mutation、authority-changing mutation、filesystem/egress/process grant 分级，避免把低影响 autosave 误做成高风险确认。
- 结论：可进入威胁模型与技术计划阶段；P0 门槛不得 waiver。
