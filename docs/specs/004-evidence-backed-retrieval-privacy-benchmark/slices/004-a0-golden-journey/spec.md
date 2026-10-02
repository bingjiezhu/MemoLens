# Feature Specification: Local Golden Journey Baseline

- Feature ID：`ML-004-A0`
- 状态：`IMPLEMENTED / CURRENT V6 BASELINE LOCALLY VALIDATED`
- 父规范：[ML-004](../../spec.md)
- 优先级：Immediate gate

## Objective

冻结一个维护者可在本机重复执行的真实最小创作旅程，作为后续媒体内核、检索、Blueprint compiler 与导出改造的红绿基线：

```text
受控临时素材
→ canonical 图片/视频导入
→ 视频 current-successful segment
→ mixed search
→ grounded legacy brief
→ Timeline 1.0 + typed revision
→ 本地 FFmpeg 720p preview
```

该基线只证明当前链条在测试 fixture 上可执行，不证明语义检索准确率、真实用户满意度、Blueprint 可编译、1080p final export、Usage Ledger 或正式发布可用。

## Current Truth

- 新媒体导入、视频 job/head、mixed search、legacy brief、Timeline revision 和 preview 已有实现与合同测试。
- canonical Blueprint 项目会诚实关闭 legacy Timeline write/render；本切片不得绕过该边界来制造“完整闭环”。
- 当前视频视觉语义主要是 local fallback；无 VLM、ASR 或气口分析时不得把技术探测描述为完整视频理解。
- 测试只使用临时 fixture、应用拥有的 cache/preview 路径，不读取维护者私人 Library。

## Requirements

- **FR-A0-001**：必须存在单一入口 `bash docs/specs/004-evidence-backed-retrieval-privacy-benchmark/slices/004-a0-golden-journey/verify.sh`。
- **FR-A0-002**：入口必须覆盖 atomic import、current analysis head、mixed retrieval、brief、Timeline revision 和 preview render。
- **FR-A0-003**：验证失败必须非零退出；缺少 FFmpeg 时测试必须显式 skip/fail，不得用伪 artifact 冒充通过。
- **FR-A0-004**：报告必须把测试通过与产品效果、final export、Blueprint compiler、发布验证分开。
- **FR-A0-005**：fixture 与产物必须位于测试临时目录；不得提交、复制或上传私人媒体。

## Success Criteria

- 验证入口整体退出码为 0。
- synthetic MP4 主链产生可校验 preview artifact；source hash、segment range、Timeline revision 与下载完整性合同保持成立。
- Node contract tests 保持 API 与工作流显示语义一致。

## Explicit Gaps

完整 ML-004 的冻结数据 manifest、nDCG/tIoU、无答案、隐私 network-deny、跨机器复现与声明 registry 不属于 A0；它们仍是后续 promotion gate。

## Local Validation Evidence

2026-08-23 首次使用隔离 runtime 运行 `verify.sh`，11 项 import service、20 项媒体主链与 10 项 renderer contract，共 41 项通过。随后 schema v6 `canonical_coverage_plan_baseline` 引入期间，该入口曾因 migration expectation 仍停在 v5 而诚实失败。

V6 合同同步后，同日使用隔离的 Python 3.14.2 / SQLite 3.53.4 对当前整合 diff 重跑同一入口，结果恢复为 11/11 import、20/20 media、10/10 Node contract，整体退出码为 0。Python 3.14 在路由类测试中仍报告未关闭 SQLite connection 的 `ResourceWarning`；它们未使本次契约失败，但依然是 lifecycle 债务，不得视为已修复。仓库既有 `.venv` 当前链接 SQLite 3.47.1，会被 `run_python.sh` 以退出码 78 正确拒绝；这仍是 setup/release 残项，不能由本次临时安全 runtime 验证消除。
