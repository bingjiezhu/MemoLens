# Feature Specification: Desktop Reliability & Verifiable Release

- Feature ID：`ML-013`
- 创建日期：2026-08-20
- 状态：`PROPOSED`
- 实施授权：`NONE`
- 组合决策：[MUST-STAGED](../implementation-decisions-2026-08-20.md)；013-A 做生命周期，013-B 在公开 beta 前交付 arm64 clean bundle，013-C/D 在 GA/更新通道前完成
- 规范类型：Spec Kit 风格的产品—架构混合规范
- 证据截止：2026-08-20
- 优先级：P0，任何公开桌面发行前门禁
- 依赖：013-A 依赖 [Spec 007-A](../007-local-capability-boundary/spec.md)；013-B 使用 [Spec 004-A](../004-evidence-backed-retrieval-privacy-benchmark/spec.md) clean-machine gate；只有 013-C 数据升级/恢复依赖 [Spec 008](../008-unified-media-memory-kernel/spec.md)
- 审计依据：[架构审计：Electron、发布与供应链](../../architecture-review-2026-08-20.md#p1-8electron-生命周期与正式发布缺口)

## Overview

MemoLens 需要把“能从源码启动”升级为“在干净机器上可安装、可验证、可升级、可回滚、不会留下孤儿进程”的桌面产品。发布单元包含 Electron app、renderer、受控 Python runtime、backend entry、FFmpeg/FFprobe、必要本地资源、schema/migration、许可证清单和诊断信息；任何核心用户旅程都不能依赖源码目录、全局 Node/Python、npm/pip、Homebrew 或首次启动联网下载。

首个公开 beta 支持矩阵为 macOS 13 及以上、Apple silicon `arm64`。Intel `x86_64` 只有在存在明确用户需求、真实测试硬件和持续维护能力时才加入支持矩阵；一旦声明支持，必须与 `arm64` 通过同等的签名、公证、clean-machine、迁移和生命周期门槛。是否提供 universal artifact 属于后续技术/分发决策。

## Hypothesis and Falsification

### Hypothesis

如果 app bundle、sidecar runtime、资源、schema 与更新都由一个签名 release manifest 管理，并对 clean install、进程生命周期、N-1 upgrade、故障迁移和 rollback 做矩阵验证，那么用户不需要开发环境也能稳定运行 MemoLens，维护者可以证明交付物来自哪次构建且不会破坏已有 library。

### Falsification

以下任一情况发生即不能公开发布：干净支持系统需要安装开发依赖；Gatekeeper/签名/公证验证失败；第二实例启动冲突 backend；退出后残留 sidecar；升级失败让旧数据和旧版本都不可用；无效更新签名被接受；release manifest 无法覆盖实际 bundle；应用依赖工作树中的文件。

## User Scenarios & Testing

### User Story 1：用户在干净 Mac 上离线安装并启动（Priority: P1）

用户从正式发布渠道下载对应 CPU 的 MemoLens `.dmg`，在没有 Node、Python、FFmpeg、Homebrew、源码或网络连接的干净 macOS 账户中安装并完成本地核心旅程。

**Why this priority**：源码启动脚本不能代表可交付产品；开发依赖会让安装结果不可复现。

**Independent Test**：在每个支持 OS/CPU 的 clean VM 上禁网，确认没有开发工具，安装 artifact 后导入一张图片与一段短视频、搜索、打开 Creator/Timeline、执行本地 preview/render，再重启。

**Acceptance Scenarios**：

1. **Given** 干净支持系统且无网络，**When** 安装并首次启动，**Then** app 不要求 npm、pip、Homebrew、Python 或 FFmpeg 安装步骤。
2. **Given** app 被移动到不同路径并以包含空格/Unicode 的用户目录运行，**When** 启动 backend 和本地媒体操作，**Then** 所有资源从签名 bundle/用户数据目录解析，不依赖源码相对路径。
3. **Given** 安装包架构与 CPU 不匹配，**When** 用户尝试安装，**Then** 发布页和安装器在执行前给出明确兼容性错误。
4. **Given** 首次启动离线，**When** optional remote/model 能力不可用，**Then** 核心本地旅程仍可用，并把可选能力标记为未安装而不是静默下载。

### User Story 2：用户只运行一个受控实例与 backend（Priority: P1）

用户重复点击 app、发生 renderer/backend crash 或退出应用时，系统只维护一个有效主实例和一个与其身份绑定的 backend，不发生端口劫持、重复 worker、孤儿进程或无限重启。

**Why this priority**：桌面 sidecar 生命周期直接决定数据一致性、安全 token 和系统资源。

**Independent Test**：并发启动两个实例，注入 renderer/main/backend crash、健康检查假阳性、端口占用、sleep/wake 和强制退出，检查进程树、lease、数据写入和恢复。

**Acceptance Scenarios**：

1. **Given** MemoLens 已运行，**When** 用户启动第二实例，**Then** 新请求被交给现有实例，且不启动第二 backend 或第二组 worker。
2. **Given** 预期 loopback 地址被其他进程占用，**When** main 启动 backend，**Then** 它不会信任未知服务，必须完成本次实例 identity challenge 或选择新的受控 endpoint。
3. **Given** backend 崩溃，**When** 自动恢复仍在预算内，**Then** main 启动同一 release manifest 中的 runtime，旧 operation 按 Spec 008 恢复，不执行 renderer 提供的命令。
4. **Given** backend 连续崩溃超过预算，**When** 进入安全模式，**Then** 自动重启停止，用户获得可操作、脱敏诊断，library 不被重复迁移。
5. **Given** 用户正常退出或更新，**When** 终止 deadline 到达，**Then** backend/worker/FFmpeg 子进程全部退出或被受控清理，不留下继续访问媒体的进程。

### User Story 3：用户安全升级并能从失败中恢复（Priority: P1）

用户从 N-1 升级到 N 时，更新包、应用版本和数据 schema 被验证；迁移失败、磁盘满、断电或新版本健康检查失败不会破坏最后一个可用数据 generation。

**Why this priority**：本地个人媒体历史不可由服务端重新生成，升级必须比功能增加更保守。

**Independent Test**：对 N-1 的最小、典型和大型 fixture 执行正常升级，在每个下载、验证、snapshot、migration、activation、first-launch 阶段注入失败，再测试升级前 rollback、升级后首次 mutation 前 rollback 和已有 N-only mutation 的恢复路径。

**Acceptance Scenarios**：

1. **Given** 有效签名且兼容的 N 更新，**When** 升级，**Then** 先验证 app/update manifest，再生成可核对的数据 snapshot 和新 schema generation，健康检查通过后才切换 active pointer。
2. **Given** 更新签名、hash、architecture 或 schema compatibility 不匹配，**When** 验证，**Then** 安装前拒绝，现有 app 与数据不变。
3. **Given** migration 任一阶段失败，**When** app 重启，**Then** N-1 与最后完整 data generation 仍可启动，或 N 进入只读恢复界面；不得继续写半迁移 schema。
4. **Given** N 已完成 N-only mutation，**When** 用户请求 rollback，**Then** 只使用预先验证的 down-migration/compatibility adapter；没有安全适配时保留 N 数据、提供只读导出和 forward-fix，不让旧 app 写入新 schema。
5. **Given** 更新过程中旧 sidecar 仍在运行，**When** 激活 N，**Then** 旧 sidecar 先完成有界关闭并释放 lease，新版本不会与旧版本并发写同一 database。

### User Story 4：维护者能验证发布来源与依赖（Priority: P1）

维护者和安全审查者可以从 release artifact 追踪代码 revision、构建身份、依赖、许可证、bundled executable、模型/资源、hash、签名、公证与 schema compatibility。

**Why this priority**：Electron、Python、FFmpeg、模型和 native libraries 形成多语言供应链，单一 npm lockfile 不足以证明交付内容。

**Independent Test**：解包 release，与 signed release manifest、SBOM、许可证和 build provenance 对比；替换任一 executable/resource 后重新验证。

**Acceptance Scenarios**：

1. **Given** 正式 artifact，**When** 执行平台签名与公证验证，**Then** app、nested executable/framework、runtime 和 helper 全部属于声明身份，Gatekeeper 接受且 notarization ticket 可离线核验。
2. **Given** release bundle，**When** 对照 manifest，**Then** bundled Python、backend、FFmpeg/FFprobe、native library、模型/资源和 schema migration 的 path、version、architecture、hash、license 均有记录。
3. **Given** 任一 bundle 文件被替换，**When** 启动前或维护者验证，**Then** signature/manifest verification 失败，文件不被执行。
4. **Given** 同一 source/lock/toolchain 的两次隔离构建，**When** 比较 pre-sign payload，**Then** normalized 差异为零或每个不可复现字段都有显式来源和审核记录。

### User Story 5：用户能获得安全诊断并控制应用数据（Priority: P2）

用户遇到启动、迁移、模型或渲染问题时，可以导出脱敏诊断；移除应用默认保留个人 library 与项目数据，只有独立确认才删除 app-owned data。

**Why this priority**：诊断不能泄露私人路径，卸载也不能意外删除用户媒体。

**Independent Test**：生成包含 Unicode/敏感文件名、provider error、migration failure 和 crash 的诊断包；执行普通卸载、重装和明确删除 app-owned data。

**Acceptance Scenarios**：

1. **Given** 私人 library，**When** 导出诊断，**Then** 包含版本、stage、稳定错误码和匿名 operation ID，不包含媒体 bytes、token、secret、绝对路径或原文件名。
2. **Given** 用户只移除 app，**When** 重装，**Then** 原媒体不被删除，app-owned data 的保留状态明确且可重新发现。
3. **Given** 用户在 main-owned 原生界面明确删除 app-owned data，**When** 删除完成，**Then** 只删除列出的 MemoLens data roots，并生成不含私人内容的结果清单。

## Edge Cases

- macOS minor update、Gatekeeper quarantine、离线公证验证和证书轮换。
- Apple silicon/Intel artifact 混用、Rosetta 缺失或 native dependency 架构不一致。
- App 位于只读卷、外置卷、路径含空格/Unicode，用户数据目录权限改变。
- 两个实例同时启动、固定端口被占、旧进程留下 PID 但实际已退出、sleep/wake 后 lease 过期。
- Renderer crash 但 main/backend 存活，main crash 但 backend 尚未收到退出信号。
- FFmpeg 派生多个进程，正常退出时子进程忽略 graceful signal。
- 更新下载中断、签名过期/撤销、manifest replay、版本 downgrade、时钟错误。
- 磁盘满、SQLite busy、migration 中断、snapshot 损坏、backup root 不可写。
- N-1 数据含 legacy projection、运行中 job、未完成 render 或已删除 preference tombstone。
- 更新后首次写入发生，再尝试运行旧 app。
- Bundle resource 存在 symlink、动态库从非 bundle 路径加载或模型 runtime 尝试执行 remote code。
- optional model 体积过大、许可证不允许再分发或首次下载被代理篡改。
- 诊断包、crash dump、update log 泄露绝对路径、query、caption 或 provider credential。
- 用户卸载 app、删除 app-owned data 与删除原媒体是三个不同动作。

## Requirements

### Functional Requirements

- **FR-001**：首个公开 beta 支持矩阵必须至少固定 macOS 13+、`arm64`，并提供 notarized `.dmg`，明确最低 OS、CPU、app version 与 schema range。只有被正式声明支持的架构才进入 production matrix；新增 `x86_64` 等架构时，必须提供同架构 artifact 并通过本规范全部适用门槛。
- **FR-002**：Packaged app 的核心离线旅程不得依赖源码目录、全局 Node/Python、npm/pip、Homebrew、系统 FFmpeg 或首次启动网络下载。
- **FR-003**：Bundle 必须包含受信 Python runtime/backend entry 与 FFmpeg/FFprobe，或包含功能等价、同样受 manifest 管理的 runtime；main 只能按 Spec 007 Runtime Manifest 启动它。
- **FR-004**：每个 bundled executable、framework、native library、model/resource 和 migration 必须记录 path、version、architecture、SHA-256、来源、license 与是否 executable/network-capable。
- **FR-005**：Optional model/resource 下载必须与 core release 分开，使用签名 manifest、content hash、明确大小/许可证/能力提示，并在无网络时不破坏核心旅程。
- **FR-006**：所有 app、framework、helper、runtime 与 nested executable 必须使用同一发布身份或声明的 designated requirement 签名，启用 hardened runtime，并只使用审计过的 entitlements。
- **FR-007**：Release 必须通过 `codesign --verify --deep --strict --verbose=2`、`spctl --assess --type execute --verbose=4` 和 `xcrun stapler validate`；验证身份、Team ID、entitlements 和结果进入 release evidence。
- **FR-008**：Notarization ticket 必须 stapled；签名、公证或 ticket 缺失时 artifact 不能标记 production-ready。
- **FR-009**：系统必须实施 single-instance 行为；第二实例只向已有主实例交付受限 open/focus intent，不启动另一 backend/worker。
- **FR-010**：Main 必须为 backend 建立本次实例专属 identity、endpoint reservation、session challenge 与 process lease；端口可被替换，但未知 loopback 服务不得因健康响应被信任。
- **FR-011**：Backend、worker、FFmpeg 与 helper 必须属于可枚举 process tree；正常退出、更新和异常退出均有有界 graceful/forced cleanup，不能按进程名称全局杀死无关进程。
- **FR-012**：自动恢复必须有 crash budget、退避和安全模式；5 分钟内连续 3 次 backend crash 后停止自动重启，避免循环迁移或外发。
- **FR-013**：Renderer crash 不得终止 committed backend operation；main crash 后 sidecar 必须在 lease/parent loss 的 10 秒内停止接受新 operation，并在 30 秒内退出或转为受控恢复状态。
- **FR-014**：用户数据、cache、logs、downloads 和 migration snapshot 必须位于声明的 app-owned data roots，与 read-only app bundle 和用户原媒体 root 分离。
- **FR-015**：更新 manifest 必须签名，并包含 version、architecture、artifact hash/size、minimum OS、from/to schema range、release channel、rollback compatibility、发布时间和 anti-replay sequence。
- **FR-016**：Update 在替换 app 前必须验证 signature、hash、architecture、version monotonicity 与 schema compatibility；失败不得改变现有 app/data。
- **FR-017**：每次 schema migration 必须从 N-1 的验证 snapshot 构建新 generation，并在 migration、integrity check 与 app health 全部通过后切换 active data pointer。
- **FR-018**：Migration 必须支持每个阶段 crash/disk-full/SQLite-busy 恢复；半迁移 generation 不得被 N 或 N-1 写入。
- **FR-019**：Release 必须说明 rollback window。N-only mutation 前必须能恢复 N-1 app/data；之后必须使用验证过的 down-migration/compatibility adapter，或保持 N data 只读并提供 forward-fix/export，禁止旧 app 盲写。
- **FR-020**：旧 sidecar、worker 和 app version 必须在新版本激活前释放 database/library lease；清理失败时更新停止而不是并发写入。
- **FR-021**：Release 必须产生机器可读 SBOM、第三方许可证汇总、漏洞审计、signed build provenance、source revision、lockfile/toolchain identity 和 artifact hashes。
- **FR-022**：Pre-sign payload 的可复现性必须在两个隔离 runner 上比较；签名时间、公证 ticket 等预期非确定字段与功能 payload 分开。
- **FR-023**：Release manifest 必须覆盖 bundle 实际文件全集；多余 executable、未声明动态库、bundle 外动态加载和 `trust_remote_code` 均为硬失败。
- **FR-024**：Fresh-install、N-1 upgrade、failed migration、pre-mutation rollback、post-mutation recovery、uninstall/reinstall 必须在最小/典型/大型数据 fixture 上形成版本矩阵。
- **FR-025**：公开 beta 对每个已声明 CPU 至少测试最低支持 macOS 与当前 release 目标版本；GA/自动更新通道至少覆盖最低版本与发布时最新三个 major version。每个已声明 CPU artifact 必须在真实或等价虚拟硬件上运行，不只检查能否解包。
- **FR-026**：Clean-machine smoke 必须覆盖 install、first launch、identity challenge、image/video import、offline search、Creator/Timeline 打开、本地 preview/render、restart 和 uninstall/reinstall。
- **FR-027**：诊断与 crash 工件必须使用 allowlist schema，只包含版本、stage、error code、匿名 identity、资源计数和脱敏状态；媒体、caption/query、token/secret、绝对路径和原文件名不得进入。
- **FR-028**：普通 app uninstall 不得删除用户原媒体或 app-owned project/data；删除 app-owned data 必须是 main-owned 原生确认的独立 operation capability，并列出精确 roots。
- **FR-029**：任何 failed release gate 都不能被普通 waiver 覆盖；仅能生成明确标记的 internal/dev artifact。
- **FR-030**：发布 pipeline、packager、updater 或签名供应商的具体选择留给技术计划，但不得改变上述 artifact、验证、兼容和恢复结果。

### Key Entities

- **Release Manifest**：版本、架构、OS/schema compatibility、文件清单、hash、签名身份和更新序列的签名权威记录。
- **Runtime Manifest**：main 可启动的 Python/backend/FFmpeg executable、固定入口、版本和 hash。
- **Resource Manifest**：模型、migration、native library 与静态资源的来源、license、architecture、hash 和能力。
- **Update Manifest**：from/to version、schema range、artifact 和 anti-replay 信息。
- **Data Generation**：迁移前后可验证、可切换且不会半写的数据库/side-index 集合。
- **Migration Snapshot**：升级前经过完整性校验、可用于 rollback 的 app-owned data 快照。
- **Process Lease**：主实例、backend 与 worker 生命周期和 database/library scope 的绑定。
- **SBOM**：Electron/npm、Python、FFmpeg/native、模型/资源依赖的机器可读物料清单。
- **Build Provenance**：source revision、builder、toolchain、lockfile、步骤和 artifact digest 的签名记录。
- **Release Evidence Bundle**：签名、公证、Gatekeeper、clean VM、upgrade/rollback、SBOM 和 smoke 的不可变验收结果。
- **Diagnostic Bundle**：使用 allowlist 字段且不含私人媒体/路径/secret 的故障工件。

## Success Criteria

### Measurable Outcomes

- **SC-001**：每个已声明支持的 OS/CPU artifact 矩阵中，fresh-install/core offline journey 通过率为 100%；缺少真实可用平台或未通过门槛的组合不得标记 supported。首个公开 beta 的强制 CPU 为 `arm64`，新增 `x86_64` 时不得降低此标准。
- **SC-002**：Clean VM 的全程 npm、pip、Homebrew、系统 Python/FFmpeg 调用次数与 core resource 网络下载次数均为 0；对源码工作树文件的成功读取次数为 0。
- **SC-003**：每个 production artifact 的 `codesign`、`spctl`、`stapler` 与 Gatekeeper launch 通过率为 100%，实际 Team ID/entitlements 与 release manifest 匹配率为 100%。
- **SC-004**：Bundle/SBOM/resource manifest 对 app 中所有 executable、native library、Python package、FFmpeg、model/resource 的覆盖率为 100%；未声明 executable 或 `trust_remote_code` 使用数为 0。
- **SC-005**：并发启动 100 次、端口占用 100 次中，第二 backend/worker 启动数为 0，未知 loopback service 被接受次数为 0。
- **SC-006**：正常退出、main crash、backend crash、update handoff 各 100 次后，30 秒时 MemoLens orphan process 数为 0，跨版本并发 database writer 数为 0。
- **SC-007**：N-1→N 的最小/典型/大型 fixture 正常 upgrade、每阶段 fault-injection、pre-mutation rollback 通过率为 100%；任何失败后最后完整 data generation 的 integrity hash 保持一致。
- **SC-008**：无效 signature/hash/architecture/schema range、replay sequence 或 downgrade 的 update 接受次数为 0；有效 update 的不可解释失败率为 0。
- **SC-009**：两个隔离 runner 的 normalized pre-sign payload hash 一致率为 100%；若 toolchain 存在不可消除差异，每项差异都有 manifest 字段、原因和安全审核，未解释差异为 0。
- **SC-010**：Release evidence 对 source revision、artifact、SBOM、license、build identity、notarization、clean VM 与 migration matrix 的可追踪率为 100%。
- **SC-011**：诊断包 100 个敏感 fixture 中，媒体 bytes、token/secret、绝对路径、query/caption 和原文件名泄露数为 0；每个失败均保留足以定位 stage 的稳定 code。
- **SC-012**：普通 uninstall 删除原媒体或 app-owned project/data 的次数为 0；明确 data-delete operation 越过声明 roots 的次数为 0。
- **SC-013**：任何 SC-001 至 SC-012 失败时 production-ready 标记次数为 0，且 artifact 只能进入 internal/dev channel。

## Release Matrix and Evidence Protocol

每个 release candidate 必须冻结：source revision、build images/toolchain、lockfiles、签名身份、notarization request/ticket、每架构 artifact、minimum/latest OS VM images、N-1 app/data fixture、schema/migration versions、network profile、optional model state和预期资源预算。

矩阵至少包含：fresh install；reinstall with existing data；N-1 clean upgrade；N-1 with active jobs；每阶段 migration kill；disk full；invalid update；rollback before N-only mutation；recovery after N-only mutation；normal quit；main/backend crash；double launch；offline launch；uninstall preserving data。每个 cell 保存机器可读结果和用户可读 failure report。

## Rollback and Degradation

- 签名、公证、manifest 或 clean-machine gate 失败时只产生 internal/dev artifact，不提供 production 更新。
- Optional model 不可打包或下载时禁用对应能力，核心本地 library/search/project 仍可打开。
- Backend crash 超预算时进入只读安全模式，不无限 restart。
- Migration 失败时保留旧 generation 和 snapshot；不能证明安全 rollback 时不启动旧 writer，只提供 N 的恢复/导出路径。
- Update service 不可用时继续运行当前已验证版本，不绕过签名手工安装未知 payload。
- 新版本回归时先停止 rollout；数据 rollback 必须遵守 FR-019，不用 app binary rollback 掩盖 schema incompatibility。

## Out of Scope

- 不在本规范中选择 electron-builder、Electron Forge、Sparkle 或更新托管商。
- 不承诺 Windows、Linux、iOS 或 Mac App Store；新增平台必须扩展同一矩阵。
- 不把可选远端 provider、模型下载或云同步作为 clean-install 核心能力。
- 不自动删除用户原媒体。
- 不保证所有 build bytes 在签名/公证后 bit-for-bit 相同；比较对象是 normalized pre-sign payload。
- 不允许发布速度覆盖数据完整性、签名或 capability 门槛。

## Assumptions

- 正式发布拥有可管理的 Apple Developer ID、notarization 与密钥轮换流程。
- Intel 支持是否长期保留由使用量和维护成本另行决策；在本规范声称支持期间必须满足完整矩阵。
- 用户数据目录可容纳 migration snapshot；空间不足时升级在写入前停止。
- 013-C 假设 Spec 008 提供可 generation 化的数据迁移和 durable operation；013-A/B 只假设 Spec 007-A 提供 runtime/data-delete capability，不等待完整 008。
- Optional 模型与其代码/权重许可证会分开审计。

## Dependencies

- Spec 004-A 保存 clean-machine、privacy 和 release run manifest；完整性能/迁移矩阵随 013-C/D 扩展。
- Spec 007-A 提供 Runtime Manifest、native confirmation、process-start 和 filesystem authority，足以独立实施 013-A/B。
- Spec 008 为 013-C 提供 schema generation、migration snapshot 与 durable operation recovery；013-A 的 single-instance/sidecar cleanup 不得被完整 008 migration 阻塞。
- CI、签名环境、notarization service 和 clean macOS runner 是执行依赖，不等同于本规范已经实现。
