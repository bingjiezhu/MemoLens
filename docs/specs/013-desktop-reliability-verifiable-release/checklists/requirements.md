# Specification Quality Checklist: ML-013

**Purpose**：验证桌面可靠性与可验证发布规范是否足以形成独立发布计划

**Created**：2026-08-20

**Feature**：[spec.md](../spec.md)

## Content Quality

- [x] 以安装、运行、升级、恢复和验证结果为中心，不预选 packager/updater
- [x] 明确支持 OS、CPU、artifact 和离线核心旅程
- [x] 区分 app binary rollback 与 data/schema rollback
- [x] 所有必需章节已完成

## Requirement Completeness

- [x] 没有待澄清占位符
- [x] Bundled Python、backend、FFmpeg、native library、模型/资源和 migration 均进入 manifest
- [x] Signing、notarization、Gatekeeper、SBOM 与 build provenance 均有可执行门槛
- [x] Fresh install、single instance、process crash、N-1 upgrade、failed migration 与 rollback 有独立 Given / When / Then
- [x] Clean machine 不依赖源码、npm/pip、Homebrew 或系统 runtime
- [x] Success criteria 有明确矩阵、分母、次数与零容忍边界
- [x] 诊断、卸载和 app-owned data 边界明确
- [x] 回退、停止发布和 internal/dev degradation 明确

## Feature Readiness

- [x] 任一 release gate 失败不会产生 production-ready artifact
- [x] 第二实例、未知 loopback 服务和孤儿 sidecar 有负向测试
- [x] Update signature、anti-replay、schema compatibility 和旧 sidecar 清理已覆盖
- [x] 规范明确不授权代码、构建或发布变更

## Review Notes

- 第 1 轮从架构审计中的 package/sign/notarize/update 缺口抽取独立规范。
- 第 2 轮补充每架构 `.dmg`、clean VM、N-1 数据矩阵、失败迁移与 rollback window。
- 第 3 轮补充 Runtime/Resource/Update Manifest、SBOM、normalized pre-sign reproducibility 和诊断隐私。
- 结论：可进入独立 release plan；在 Spec 004、007、008 实现前不能声称已具备公开发行条件。
