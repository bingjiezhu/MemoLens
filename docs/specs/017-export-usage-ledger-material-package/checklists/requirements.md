# Specification Quality Checklist: Export Usage Ledger & Material Packages

**Purpose**：验证 ML-017 是否把导出、精确使用记录和本地素材包闭合成可测流程。

**Created**：2026-08-22
**Feature**：[spec.md](../spec.md)

## Content Quality

- [x] 以用户一次导出即可完成记录的结果为中心。
- [x] 区分产品行为与未来容器/事务实现选择。
- [x] 所有 requirement 可验证，没有模糊“安全打包”。
- [x] 没有 `[NEEDS CLARIFICATION]`；A/A2/A3 staged 实施与未完成父范围分开记录。

## Decision Alignment

- [x] 成功导出时自动记录，不再强制确认“已发布”。
- [x] 默认包包含成片、脚本/字幕/封面（若有）、TXT 和 manifest。
- [x] 轻量包不复制/移动原件。
- [x] 完整包按需复制实际使用图片与视频片段，仍不动原件。
- [x] 仅精确 used interval 被标记，长视频 remainder 可继续使用。
- [x] 成片默认从 raw 素材候选排除。
- [x] 全流程仅本地文件夹，不引入云。

## Integrity and Safety

- [x] usage 来自验证 Timeline/source mapping，不由模型猜测。
- [x] 成功与失败/部分导出具有原子、可恢复语义。
- [x] manifest、TXT、package derivative 与原件身份明确分离。
- [x] path 不是 identity；移动、离线和替换均有测试。
- [x] overwrite/full package 使用准确 scoped confirmation。
- [x] 第三方资产复制遵守许可边界。

## Requirement Completeness

- [x] 用户故事覆盖导出、后续检索、轻量包、完整包与修正。
- [x] Edge cases 覆盖复杂时间映射、代理、文件系统和并发。
- [x] 成功标准覆盖准确性、原子性、可读性、迁移、隐私和幂等。
- [x] 有明确 degradation 与 unsupported 行为。

## Implemented A/A2/A3 gate

- [x] 维护者已批准 ML-017 的 staged A/A2/A3 切片。
- [x] Canonical Export Revision、source mapping、success-only Usage 和 durable commit attestation contract 已固定。
- [x] 当前轻量包的 logical schema、TXT projection、completion protocol 和 no-overwrite publication 已固定；full package/relink 仍待后续切片。
- [x] Renderer actual-read 对照、exact interval、residual lineage 和 historical cold replay fixtures 已建立。
- [x] 首版只支持线性 hard-cut/image 映射；speed/reverse/freeze/nested 保持明确 unsupported。
- [x] A 的 exact native target、held `dir_fd`、no-overwrite staging/cleanup 和不复制第三方原媒体边界已通过本地验证。
- [x] A3 deterministic residual identity、same-transaction admission、五表 cold audit 和 successful-output exact-SHA exclusion 已通过 controlled-local gates。

## Remaining parent / ML-017-B planning and promotion gate

- [ ] 维护者批准 ML-017 remainder/ML-017-B 的具体实施切片。
- [ ] ADR 固定 full package、source locator/relink、Usage correction/supersession 与 materialized Wiki 的 logical contract。
- [ ] 建立 full-package renderer-read 对照与 speed/reverse/freeze/nested interval property fixtures。
- [ ] 完成 full-package overwrite/relink/staging cleanup 和第三方资产复制 threat model。
- [ ] Fresh A2 polling/admission/V9 native Electron journey。
- [ ] Codex/DeepSeek 真实 model-call/host-UI 验收、Remote CI 与 release。
- [ ] 字幕、封面、转场、source-audio edit/mix、audio stream/content/sync attestation 和 final-fidelity render。

只有经验证的 successful canonical export 才可以原子写入 exact Usage。当前 A/A2/A3 的 controlled-local 实现不会自动升级未勾选的父范围，也不允许复制、移动或删除用户原媒体。
