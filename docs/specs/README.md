# MemoLens Specs

本目录保存 MemoLens 的产品规范与架构演进提案。当前仓库沿用 `docs/specs/`，这是本项目选择，不是 GitHub Spec Kit 默认的根目录 `specs/` 约定。

当前公开版本先读 [2026-10-02 发布与可用性记录](../releases/2026-10-02-readiness.md) 和 [使用指南](../user-guide.md)。历史决定与剩余范围见 [2026-09-04 全局复核](../audits/2026-09-04-convergence/overview.md) 与 [逐项要求](../audits/2026-09-04-convergence/requirements.md)；历史测试数字仍以各自证据日期为准，私有会话不随公共仓库发布。

## 规范状态与组合决策

规范的 Proposal 状态不代表整份规范都应进入下一个 backlog。2026-08-20 的代码对照和架构裁决见 [Spec ↔ 实现决策复核](implementation-decisions-2026-08-20.md)；[43 问产品对齐记录](product-decisions-43-questions-2026-08-22.md) 是历史产品决定基线，后续 Codex 界面优先、DeepSeek Harness 和视觉恢复等明确修正见 [逐项要求 Q1–Q43 / H01–H15](../audits/2026-09-04-convergence/requirements.md)。2026-08-23 的组合取舍、历史规范退役、依赖和 V0–V8 纵向顺序见 [Spec Portfolio Convergence](portfolio-convergence-2026-08-23.md)。冲突时以较新的明确用户决定为准，仅覆盖它实际修正的范围；已证实的安全、身份、原件不动、可恢复和 canonical ledger 约束继续有效。

当前 portfolio 只保留一条内容权威链：

```text
Canonical Media Ledger
  → Wiki Generation / exact evidence
  → Creative Blueprint Revision
  → Coverage Plan Revision
  → Timeline Revision
  → Export Revision
  → Usage Facts / residual intervals
```

Creator Context、Research Snapshot、Technique Set 和 Capability Profile 是固定的正交输入；Operation Ledger 只记录状态变更，不是第二份内容真源。Legacy brief/storyboard 只作迁移输入或 projection，独立 Story Graph 不进入当前已承诺主链。

| 编号 | 规范 | 实现状态 | 组合决策 | 作用 |
| --- | --- | --- | --- | --- |
| 004 | [Evidence-backed Retrieval & Privacy Benchmark](004-evidence-backed-retrieval-privacy-benchmark/spec.md) | Partially implemented; [A0 golden journey](004-evidence-backed-retrieval-privacy-benchmark/slices/004-a0-golden-journey/spec.md) and [A1 production inventory/offline deny](004-evidence-backed-retrieval-privacy-benchmark/slices/004-a1-production-surface-inventory-offline-deny/spec.md) locally validated within declared scope | CONTINUOUS EVIDENCE GATE / P0 | 已有临时媒体黄金旅程、production action inventory、exact negative oracles 与进程范围 offline deny；完整冻结数据、检索质量评测、跨机器复现和声明 registry 仍未完成；不等于 OS 全进程网络证明或产品质量收益 |
| 005 | [Video Creative Workbench](005-video-creative-workbench.md) | Partially implemented / validation required | HISTORICAL / PENDING MIGRATION RETIREMENT | 保住 grounded evidence、typed Timeline、validation 和 preview；分别迁入 008/015/017，不继续把该大规范扩为新真源 |
| 006 | [Creator Memory & Media Inbox](006-creator-memory-media-inbox.md) | Implemented / not validated | HISTORICAL / PENDING MIGRATION RETIREMENT | 保住已实现行为；Inbox 后续归 008/Library，Creator Memory 后续归 011；黄金旅程过门前不退役 |
| 007 | [Local Capability Boundary](007-local-capability-boundary/spec.md) | Partially implemented; [A0 current boundary](007-local-capability-boundary/slices/007-a0-current-boundary-closure/spec.md) and shared [007-A1 inventory/offline gate](004-evidence-backed-retrieval-privacy-benchmark/slices/004-a1-production-surface-inventory-offline-deny/spec.md) locally validated within declared scope | MUST-STAGED / P0 BOUNDARY | runtime、root/DB/source、renderer/main、Agent authority 已有回归基础；共用 A1 补齐 production census 与发送前 offline deny。完整 provider 授权/撤销/删除闭环、真实 native/provider 接线及未覆盖 adapter 仍须验收；不证明同 UID 恶意进程隔离 |
| 008 | [Unified Media Memory Kernel](008-unified-media-memory-kernel/spec.md) | Partially implemented; [A0.1 canonical image bridge](008-unified-media-memory-kernel/slices/008-a0-canonical-image-bridge/spec.md) implemented / controlled-local gates passed / promotion blocked | MUST-STAGED，最高架构优先级 | 已实现 canonical image publication、durable analysis job、单向 shadow projection、consumer read cutover 与有界 provider grant 合同；historical RED 工件、真实 native/provider/host、大型用户库、clean-machine/CI/release 仍未验收；versioned embedding/projection 与父级其余范围继续分期 |
| 009 | [Intent Compiler & Evidence Federation](009-intent-evidence-retrieval/spec.md) | Proposed / Experiment | MUST-STAGED / 009-A Core | 先统一 typed intent、honest empty 与 evidence contract |
| 010 | [Hierarchical Temporal Memory](010-hierarchical-temporal-memory/spec.md) | Proposed / Experiment | EMBED MINIMUM / ADVANCED EXPERIMENT | 精确 span、temporal observation 与 bounded refinement 嵌入 008/014/018；full event/episode/concept graph 不进已承诺主线 |
| 011 | [Consentful Creator Model](011-consentful-creator-model/spec.md) | Proposed / Experiment | MUST-STAGED / 011-A Context Compiler | 008-A/009-A 后修复 Creator 字段分流；提供遗忘承诺前补 Full Forget；主动学习留在实验 |
| 012 | [Verifiable Story Compiler](012-verifiable-story-compiler/spec.md) | Proposed / Experiment | EMBED CLAIM CONTRACT / PAUSE STORY GRAPH | 四类 claim、evidence 和 review-required 嵌入 Blueprint/Coverage/Export；不新建独立 canonical Story Graph，C2PA 后置 |
| 013 | [Desktop Reliability & Verifiable Release](013-desktop-reliability-verifiable-release/spec.md) | Partially implemented; [A0 lifecycle baseline](013-desktop-reliability-verifiable-release/slices/013-a0-lifecycle-baseline/spec.md) implemented / locally validated within source-tree lifecycle | MUST-STAGED / RELEASE GATE | 已冻结 backend identity、startup 合并、bounded retry、stop/kill、job interruption recovery 与后台 polling 生命周期；整个桌面 single-instance、clean arm64 bundle、签名/公证、N-1 migration、升级/回滚与发布矩阵仍未证明 |
| 014 | [Agent-Navigable Media Wiki](014-agent-navigable-media-wiki/spec.md) | A0 implemented / validated | MUST-STAGED / P0 底座 | [014-A0](014-agent-navigable-media-wiki/slices/014-a0-live-read-surface/spec.md) 已交付 live 导航与精确证据读取；后续是 materialized generation/pinning、bounded multimodal refinement 与 traversal benchmark |
| 015 | [Agent-Agnostic Creative Protocol & Open Project Chain](015-agent-agnostic-creative-protocol/spec.md) | A0+A1+B0+B1 implemented / validated; [A2 plugin-first bootstrap](015-agent-agnostic-creative-protocol/slices/015-a2-plugin-first-library-bootstrap/spec.md) Phase 0–3 implemented / validated controlled-local; real Library-to-editor acceptance pending; B1A local GO; B2A implemented with residuals; B2B+B2B2+B2B3 local validation with residuals; B2B4+B2C0 implemented / parent promotion blocked; [B2B4A](015-agent-agnostic-creative-protocol/slices/015-b2b4a-verified-visual-replacement-review/spec.md)+[B2B4B](015-agent-agnostic-creative-protocol/slices/015-b2b4b-safe-canonical-playback-grant/spec.md)+[B2B4B.1](015-agent-agnostic-creative-protocol/slices/015-b2b4b1-preview-transport-audio-truth/spec.md)+[B2B4C](015-agent-agnostic-creative-protocol/slices/015-b2b4c-canonical-structural-edit/spec.md) validated controlled-local | MUST-STAGED / P0 主链 | Codex/DeepSeek 共用 Browser Canonical Editor 现已有 move/trim/image-duration/replace、独立授权的 video Split 与 Remove from Timeline、read-only K、显式 K→N+1 restore 和 project/head/clip-bound raw preview。Raw MP4 transport 可能含音频，页面禁止 audio playback 并保持 output muted；不证明 stream/content/mix/final fidelity。Electron 只是 native authority/runtime/broker 与兼容 reader，不是主剪辑界面。controlled-local Codex AAC Browser 已跑，DeepSeek 只完成 fresh loader；真实双向 host-model-UI、DeepSeek AAC Browser、native audio-device、Remote release、跨资源 unified history、audio edit/mix/subtitle 与 final-fidelity preview 仍未闭合 |
| 016 | [Craft Wiki & Technique Compiler](016-craft-wiki-technique-compiler/spec.md) | Proposed / Experiment | EXPLAIN FIRST / COMPILE BY EVIDENCE | 先建 Research Snapshot/Reference Pin 和有来源 Technique Card；只有 capability/validator/render fixture 通过才编译 typed edits |
| 017 | [Export Usage Ledger & Material Packages](017-export-usage-ledger-material-package/spec.md) | [A canonical export/usage](017-export-usage-ledger-material-package/slices/017-a-canonical-export-usage-ledger/spec.md)+[A2 Usage projection/admission](017-export-usage-ledger-material-package/slices/017-a2-usage-projection-admission/spec.md)+[A3 exact residual/derivative exclusion](017-export-usage-ledger-material-package/slices/017-a3-exact-residual-derivative-exclusion/spec.md) validated controlled-local; parent partially implemented | MUST-STAGED / P0 闭环 | A 已交付 native authority、durable attestation、五角色 1080p silent package 与 success-only Usage，并观察到真实 Electron native success；A2 已交付 used/residual Search projection、strict Renderer adoption、事务内 Brief admission 和 bounded polling；A3 已交付 deterministic residual identity、legacy/canonical exact lowering、五表 cold audit 与 successful-output exact-SHA exclusion。fresh A2 native journey、真实 Codex/DeepSeek model-host journey、Remote CI/release、字幕/封面/转场、audio edit/mix/attestation/final fidelity、full package/relink/Usage correction/Wiki 仍未闭合 |
| 018 | [Script Coverage & Global Footage Assignment](018-script-coverage-global-footage-assignment/spec.md) | [A1 baseline](018-script-coverage-global-footage-assignment/slices/018-a1-canonical-coverage-plan/spec.md) implemented / local validation with residuals; parent Proposed / Experiment | MUST-STAGED EXPERIMENT / P0 差异化 | A1 已提供唯一 canonical Coverage baseline 与可检查 UI；Audio Signals、Global Assignment 和 local replan 仍分期，只有完整 preview paired benchmark 过门才替换 baseline；见 [A1 实施证据](018-script-coverage-global-footage-assignment/slices/018-a1-canonical-coverage-plan/implementation-evidence.md) |

总体处置与用户可观察验收见 [Spec Portfolio Convergence](portfolio-convergence-2026-08-23.md)，依赖和分阶段顺序见 [Next-generation Roadmap](roadmap.md)。代码审计证据见 [2026-08-20 架构审计](../architecture-review-2026-08-20.md)，通用技术候选见 [2026-08-20 前沿调研](../frontier-research-2026-08-20.md)，Google、OriNodes-v4、开源项目与学术资料的专项结论见 [2026-08-22 Agent 可导航媒体 Wiki 调研](../agent-media-wiki-research-2026-08-22.md)。

## Spec Kit 风格与偏离声明

这些文档是 **Spec Kit 风格的产品—架构混合规范**，不是官方 `/specify` 生成的严格 technology-agnostic feature spec。它们采用 Spec Kit 的用户故事、独立验收、需求清单和可量化成功标准，同时保留本次代码审计已经证明不可回退的安全、身份、可恢复性与发布完整性约束。这样做是为了先把产品结果和架构风险放在同一决策包里，而不是声称完全符合官方 WHAT/WHY 边界。

进入实施前，每份混合规范必须拆成两层：用户可观察行为和结果留在 feature spec；候选机制、数据结构、并发协议、hash 拓扑与组件边界移入 `plan.md`、research 或 ADR。当前 checklist 验证的是提案完整性与可证伪性，不代表“没有实现细节”，也不代表实现已获授权。

当前采用的 Spec Kit 核心质量要求包括：

- 每个用户故事有优先级，并可独立验收。
- 验收场景使用 Given / When / Then。
- 功能要求优先描述行为和结果，不预选具体框架、数据库、模型、packager 或 updater；审计导出的架构不变量会明确视为候选约束，并在后续 plan/ADR 中拆分和确认。
- 成功标准可量化、可复现，并尽量保持技术无关。
- 边界、假设、依赖和不做事项明确。
- 每份规范有独立的 `checklists/requirements.md`。
- 前沿能力额外声明假设、基线、证伪条件和降级路径。

官方参考：

- [GitHub Spec Kit](https://github.com/github/spec-kit)
- [Spec template](https://github.com/github/spec-kit/blob/main/templates/spec-template.md)
- [Specify command and quality checklist](https://github.com/github/spec-kit/blob/main/templates/commands/specify.md)
- [Spec-driven development](https://github.com/github/spec-kit/blob/main/spec-driven.md)
- [Agentic SDD workflow](https://github.com/github/spec-kit/blob/main/docs/reference/agentic-sdd.md)
- [Spec of Specs](https://github.com/github/spec-kit/blob/main/docs/concepts/spec-of-specs.md)

## 当前授权边界

2026-08-22 用户已授权将 Grill Me 达成的共识按小切片实现；2026-08-23 用户进一步授权按 [Spec Portfolio Convergence](portfolio-convergence-2026-08-23.md) 调整后的 V0–V8 组合顺序持续实施，并允许在不背离已决产品原则的前提下收窄、合并、拆分或终止证据不足的方案。这是 staged implementation authorization，不是对未实现状态的更名：每个切片仍必须单独完成 spec/plan/tasks、迁移/回退和用户可观察验收，才能升级为 `Implemented` 或 `Validated`。

ML-014-A0 与 ML-015-A0/A1/B0/B1 已实现并通过已记录验证；[ML-015-A2 plugin-first bootstrap](015-agent-agnostic-creative-protocol/slices/015-a2-plugin-first-library-bootstrap/spec.md) 已到 `IMPLEMENTED / PHASE 0-3; VALIDATED CONTROLLED LOCAL`：V20 Core bootstrap、可恢复 scan 和空库 editor gate 已通过本地验证；真实 Library→grounded Timeline→editor 旅程仍待验收；ML-015-B1A maintenance 为本地 `LOCAL GO`；ML-015-B2A 已实现但仍有 Desktop/视觉/hosted CI residual。B2B3/B2B4/B2C0 已交付基本 closed edits、paired Timeline write、共用 Browser Canonical Editor、read-only K 和 append-only restore；B2B4A/B/B.1/C 增加 verified visual replacement、独立授权的 source preview、诚实 AAC transport/muted-output 合同、video Split 与 Remove from Timeline。Electron 仅作 native authority/runtime/broker 和兼容 reader。controlled-local Codex AAC Browser 已跑，DeepSeek 仅完成 fresh loader。ML-017-A3 也已交付 exact residual identity 与 successful-output exact-SHA exclusion。真实 Codex↔DeepSeek 双向 model/UI journey、DeepSeek AAC Browser、native audio-device、Remote release，以及完整 B2C unified history、source-audio edit/mix、stream/content attestation、subtitle/audio-level、final-fidelity preview、full package/relink/Usage correction/Wiki 仍未完成，因此仍不能写成 V3/V4 complete。

组合授权也不扩张任意文件权限。导出、覆盖、完整包、迁移、移动/归档或删除仍必须经对应切片的 scoped capability、故障注入和可恢复验收；原始媒体默认永不移动、删除、覆盖或上传。ML-005/006 等历史路径在 successor 合同、迁移、零 production caller 与 retirement manifest 完成前不可移除；完成后只以可恢复的垃圾桶/待退役方式处理。

0.10.1 只是 maintenance candidate；[B1A 实施证据](015-agent-agnostic-creative-protocol/slices/015-b1a-authority-runtime-hardening/implementation-evidence.md) 记录了 B1A 42、B1 相关 78、Blueprint 28、plugin 228 项聚焦结果，独立 `P0=0 / P1=0 / LOCAL GO`，以及最终 diff 上两轮完整 `npm run check`（每轮 Core/unit 289、plugin 228、Node combined 86、renderer models 46）与 0 个已知漏洞的 dependency audit。该状态不等于已发布或 hosted validation；GitHub-hosted CI 未运行。

进入实施前，维护者应逐项执行：

1. 选择一个规范并确认范围。
2. 运行或复核该规范的需求 checklist。
3. 为该规范单独生成技术计划、数据迁移策略和任务拆分。
4. 先记录现状 benchmark，再开始改动。
5. 保留旧路径作为可观测、可回滚的过渡层，直到对应退出门槛全部通过。

## 状态词义

- `Proposed`：规范已成形但尚未进入确定的实施计划；是否已获 staged implementation authorization 必须另读“当前授权边界”，不由该状态词单独推断。
- `Experiment`：必须先跑预注册实验；未达到成功标准时终止或降级。
- `Planned`：范围和计划已批准，但还未实现。
- `Implemented`：代码存在，不等于已经通过所有发布门槛。
- `Validated`：实现和独立验收均通过，可用于结果型声明。
- `Deprecated`：仅为迁移兼容保留，已有明确删除门槛。

任何性能、隐私、检索质量或创新性宣传，只有在 Spec 004 冻结数据、基线、运行环境和原始工件后，才能从“设计目标”升级为“项目结果”。
