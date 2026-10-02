# Feature Specification: Local Capability Boundary

- Feature ID：`ML-007`
- 创建日期：2026-08-20
- 状态：`PROPOSED`
- 实施授权：`NONE`
- 组合决策：[MUST-NOW / 007-A Core](../implementation-decisions-2026-08-20.md)；先修 runtime、legacy indexing、root/DB、原件读取与外发，不先建设通用策略平台
- 规范类型：Spec Kit 风格的产品—架构混合规范
- 证据截止：2026-08-20
- 优先级：P0，公开发布前门禁
- 依赖：[Spec 004](../004-evidence-backed-retrieval-privacy-benchmark/spec.md)
- 审计依据：[架构审计 P0](../../architecture-review-2026-08-20.md#5-p0发布与架构门禁)

## Overview

MemoLens 的 renderer、Electron main、Flask backend、Codex plugin 和 Photon Bot 都运行在同一用户权限下，但它们不应拥有相同能力。系统需要把命令启动、library 切换、数据库选择、文件读取、模型外发、导出和聊天附件定义为显式、最小、可撤销、可审计的 operation capability。

本规范处理的是 confused-deputy 和受信表面被攻破后的能力放大，不宣称存在无条件远程利用或本地提权。正常用户主动选择 library、启动合法 runtime、发送允许的照片仍是产品功能。

## Hypothesis and Falsification

### Hypothesis

如果高影响操作只能通过 operation、root、asset、purpose 和时间范围明确的 capability 执行，那么即使受信 renderer、Bot response 或本地请求参数被控制，系统仍能阻止越界文件读取、任意 executable 选择、跨库写入和未授权 provider egress。

### Falsification

在 compromised-surface 测试中，只要攻击者能凭 session token、Origin、raw path、raw DB path 或持久化 settings 任一项获得未明确授权的高影响能力，本假设即失败。仅靠 UI 提示或日志告警不算隔离成功。

## User Scenarios & Testing

### User Story 1：用户安全启动本地服务（Priority: P1）

用户启动 MemoLens 时，桌面主进程只运行应用认可的 backend runtime；网页代码不能把任意 executable 持久化为以后启动的程序。

**Why this priority**：进程启动是 main-process 能力，影响范围高于普通设置保存。

**Independent Test**：在受信主 frame 中尝试保存合法 runtime、任意文件、命令字符串、目录、symlink 和篡改后的 settings，再观察 main 是否只启动允许 runtime。

**Acceptance Scenarios**：

1. **Given** production build，**When** renderer 提交 raw executable path，**Then** main 拒绝请求，且不产生进程启动。
2. **Given** settings 文件被手工篡改为不允许的 executable，**When** 应用加载，**Then** 值被隔离并回退到安全 runtime。
3. **Given** 合法 bundled runtime，**When** 用户启动 backend，**Then** executable 和固定入口参数与受信 manifest 一致。
4. **Given** 开发模式 override，**When** 应用重启为 production，**Then** override 不被持久化或继承。

### User Story 2：用户只分享批准范围内的媒体（Priority: P1）

用户通过 Photon 或其他外发渠道分享搜索结果时，系统只发送已批准 asset 的重新编码安全副本；library 内 symlink、污染路径、转码失败或伪装文件不能导致 root 外内容或原文件被发送。

**Why this priority**：私人媒体外发不可逆，失败时必须 fail closed。

**Independent Test**：构造普通图片、`..`、绝对路径、文件 symlink、中间目录 symlink、TOCTOU 替换、损坏图片和转码失败，使用网络 mock 观察实际附件字节。

**Acceptance Scenarios**：

1. **Given** root 内普通图片和有效分享操作，**When** 准备附件，**Then** 输出是新的受限媒体工件，不是原始文件路径。
2. **Given** root 内指向 root 外的 symlink，**When** 请求分享，**Then** 不读取、不上传任何目标字节。
3. **Given** 转码器不可用或失败，**When** 准备附件，**Then** 跳过附件并报告安全错误，不回退到原文件。
4. **Given** 检查后路径被替换，**When** 文件被打开，**Then** identity mismatch 使操作失败。

### User Story 3：用户明确授权 library 与高影响操作（Priority: P1）

用户选择 library、切换数据库、导出或重建索引时，授权只覆盖当前操作、明确 root 和 active database identity；其他表面不能把 raw path 当作持久权限。

**Why this priority**：当前严格的文件 API 仍可能被 settings 重授权绕开。

**Independent Test**：已持有 renderer session token 的测试客户端尝试切换任意 root/DB、写其他 SQLite、重建未授权 library 或重复使用旧授权。

**Acceptance Scenarios**：

1. **Given** session token 但没有 library-change capability，**When** 请求切换 root 或 DB，**Then** 请求被拒绝。
2. **Given** 一次原生用户确认产生的 library capability，**When** 完成指定切换，**Then** capability 失效，不能复用到其他路径。
3. **Given** 旧 library 的异步操作，**When** active library 已切换，**Then** 操作不能写入新 library 或新 database。
4. **Given** raw absolute DB path，**When** 任一 surface 用它作为 authority，**Then** 系统拒绝并要求 opaque database identity。
5. **Given** 首次导入还没有 asset ID 或内容 hash，**When** 用户在 main-owned 原生界面批准一个 root discovery，**Then** 系统只做有界枚举，原子捕获相对条目、device/inode、size 和 mtime，并创建 opaque source manifest。
6. **Given** 已有 source manifest，**When** 后续 decode、分析或导出，**Then** 操作使用 source ID 和内容 hash，不重新接受 renderer 提交的 raw path。
7. **Given** 受损 renderer，**When** 它直接请求签发 grant、提交 `confirmed=true`、篡改 operation digest 或重放旧确认，**Then** policy owner 不签发有效 capability。

### User Story 4：用户知道哪些内容会发给模型 provider（Priority: P1）

远端分析前，用户能看到 provider、用途、媒体/字段范围和有效期；系统只发送授权 payload，并记录可核对但不泄露内容的 disclosure。

**Why this priority**：照片、caption、embedding、人物关系和偏好都可能包含敏感信息。

**Independent Test**：对无授权、过期授权、重复使用、扩大 asset scope、改变 provider/model 和 payload hash 的请求逐一验证。

**Acceptance Scenarios**：

1. **Given** 没有有效 grant，**When** adapter 准备远端请求，**Then** 在网络发送前硬失败。
2. **Given** single-use grant 且网络发送前发生确定性校验或 decode 失败，**When** 用户修正本地问题后重试同一 payload，**Then** grant 尚未消费。
3. **Given** 第一字节已开始发送、请求超时或响应丢失，**When** 客户端尝试自动重试，**Then** grant 处于 consumed/ambiguous，系统不再次外发。
4. **Given** 上次请求处于 consumed/ambiguous，**When** 用户再次确认同一用途，**Then** policy owner 签发新的 grant，旧 grant 仍不可复用。
5. **Given** 请求的 payload scope 或 hash 与 grant 不同，**When** 校验，**Then** 请求被拒绝。
6. **Given** 授权请求完成，**When** 用户查看活动记录，**Then** 能看到 provider、model、purpose、asset scope、payload classes、bytes、hash、时间和结果。

### User Story 5：维护者可以审计每条本地 API 能力（Priority: P2）

维护者新增或修改 route、IPC 或 Bot action 时，自动检查能确定它是只读、mutation、文件读取、进程启动还是 egress，并验证对应门槛。

**Why this priority**：手工维护 allowlist 容易让新 route 漏掉安全规则。

**Independent Test**：枚举全部 surface contract，故意加入一个未声明 mutation，质量门禁应失败。

**Acceptance Scenarios**：

1. **Given** 未声明 capability class 的新操作，**When** contract test 运行，**Then** 构建失败。
2. **Given** 普通 domain mutation，**When** 缺少 session/library binding、revision/CAS 或幂等语义，**Then** contract test 失败。
3. **Given** authority-changing/high-impact mutation，**When** 缺少短期 operation capability，**Then** contract test 失败。
4. **Given** filesystem read、egress 或 process-start，**When** 缺少对应 root/source、payload 或 runtime grant，**Then** contract test 失败。

## Edge Cases

- Renderer 与 main 同 origin，但 renderer 代码被 XSS 或供应链注入控制。
- 用户主动选择自定义 Python，但路径后来被 symlink 替换或文件签名变化。
- Backend 已经健康，恶意 settings 只在以后重启时生效。
- Library root 本身被移动、权限改变、卸载或 inode 变化。
- 路径包含 Unicode normalization、case-insensitive alias、hard link 或 mount boundary。
- 校验与读取之间发生 rename、replace 或 symlink swap。
- Provider 请求超时，无法确定服务端是否已收到 payload。
- Grant 已使用，但客户端没有收到 response。
- Operation capability 被复制到另一个 library、database、surface 或用户会话。
- Originless 本机脚本、受信 dev origin、file renderer 与生产 renderer 的权限差异。
- Preview、thumbnail、original、transcript 和 embedding 被错误归到同一敏感级别。
- Bot allowlisted 用户在共享 channel 中请求另一位用户的 session 内容。
- Log、health 或 error envelope 泄露绝对私人路径和文件名。
- 用户撤销授权时仍有运行中 job、缓存 payload 或重试队列。

## Requirements

### Functional Requirements

- **FR-001**：每个 IPC、HTTP route、MCP tool 和 Bot action 必须声明 capability class：read、mutation、filesystem-read、process-start、egress 或其明确组合。
- **FR-002**：未声明 capability class 的操作不得进入 production build。
- **FR-003**：Origin、sender identity 和 session token 只能作为调用者证明，不能单独授予高影响操作。
- **FR-004**：普通 domain mutation 必须绑定 session、active library/database identity，并使用 revision/CAS 与幂等语义；它不要求每次弹出独立高影响确认。
- **FR-005**：改变 library/database/root/provider authority、批量破坏性操作等 high-impact mutation 必须额外绑定短期 operation capability；filesystem-read、egress 和 process-start 分别绑定 root/source、payload 和 runtime grant。
- **FR-006**：首次 library discovery 必须由 main-owned 原生确认签发只允许有界枚举的 root discovery capability；枚举必须原子捕获 normalized relative entry、device/inode、size、mtime 和 root identity，并创建 opaque source manifest/source ID。
- **FR-007**：首次 discovery 之后的 decode、分析、preview、original、export 和 egress 必须绑定 source ID 与预期 content hash；raw path 不得重新成为权限证明。
- **FR-008**：系统必须在文件打开时验证每个真实路径组件、文件类型和 source identity，防止 symlink 与 TOCTOU 绕过。
- **FR-009**：Renderer 不得保存或选择 main 将直接执行的 raw executable command。
- **FR-010**：Production runtime 必须来自受信 manifest 或经过独立原生确认的安全选择；开发 override 不得持久化到 production settings。
- **FR-011**：IPC 与其他高权限 surface payload 必须在权限所有者一侧进行运行时 schema 校验。
- **FR-012**：Process-start、root/database authority change、provider egress 和 original export grant 只能由 Electron main/backend policy owner 签发；renderer 不得持有签发 secret 或自行构造有效 grant。
- **FR-013**：需要用户确认的 grant 必须来自 main-owned native surface，绑定不可伪造 nonce、当前 operation digest、明确 user gesture、subject session 和 expiry；renderer 提交的布尔“已确认”不构成确认。
- **FR-014**：Grant 签发、校验、消费和对应 operation 状态变更必须在 policy owner 的同一原子状态转换中完成，防止双花、重放和检查后替换。
- **FR-015**：Capability 必须具备唯一 ID、issuer、subject/surface、operation、scope、operation digest、issued/expiry、single/multi-use 状态和撤销状态。
- **FR-016**：Capability 不能通过改变 raw path、DB path、provider、model、asset set、payload class 或 operation digest 扩大范围。
- **FR-017**：所有远端 provider egress 必须在发送前匹配未过期 grant 和 payload manifest。
- **FR-018**：Payload manifest 必须记录 provider、model、purpose、asset/source IDs、payload classes、bytes、content hash、创建时间和发送结果。
- **FR-019**：网络发送前的确定性 preflight/decode 失败不得消费 grant；一旦开始发送第一字节、超时或响应丢失，grant 必须原子进入 consumed/ambiguous，禁止自动重发；再次发送需要新的原生用户确认和新 grant。
- **FR-020**：未经授权的照片、视频帧、音频、transcript、caption、embedding、人物图和 Creator Memory 发送数必须为零。
- **FR-021**：聊天附件必须由受信媒体读取边界从 source/asset identity 生成新的受限工件，不得把原始文件路径直接交给外部 SDK。
- **FR-022**：附件 decode、验证、重编码或尺寸限制任一步失败时必须 fail closed。
- **FR-023**：Preview 与 original 的读取规则必须明确区分；originless 本机调用不得自动获得私人 preview。
- **FR-024**：受损 renderer 即使持有 session token，也不能自助签发 grant、伪造/重放 native confirmation、切换 root/DB、读取未授权 root、外发未授权资产或启动任意 executable。
- **FR-025**：Settings 修改不得隐式创建新的 library、database、filesystem、process-start 或 egress authority。
- **FR-026**：Capability 撤销必须阻止新的读/写/发送；运行中操作必须在安全 checkpoint 停止或记录不可撤回阶段。
- **FR-027**：安全事件日志必须包含 operation/capability/result，不包含原始媒体、token、payload 或不必要的绝对路径。
- **FR-028**：系统必须对每种能力提供拒绝原因和恢复动作，不得以静默 fallback 扩大权限。
- **FR-029**：所有 capability 判定必须由共享 policy contract 驱动，不能由各 route 或 adapter 自行复制规则。
- **FR-030**：安全策略变化必须有 migration 与 backward-compatibility 行为；旧客户端不能因缺字段自动获得宽松权限。
- **FR-031**：本规范的 action inventory、issuer abuse、forgery、replay、symlink、TOCTOU 和 egress negative tests 必须进入发布门禁，不能用 waiver 绕过。

### Key Entities

- **Surface Identity**：renderer、main、local API client、Codex plugin 或 Bot adapter 的调用身份。
- **Library Identity**：不暴露路径的稳定 library scope。
- **Database Identity**：与 library 和 schema generation 绑定的 active database UUID。
- **Root Capability**：允许在特定 root、operation 和时间范围内访问 asset 的授权。
- **Root Discovery Capability**：只允许首次、有界枚举并冻结文件身份的短期原生授权。
- **Source Manifest**：首次发现时冻结的 root、相对条目、device/inode、size、mtime、后续 content hash 和 opaque source ID。
- **Operation Capability**：一次或有限次数执行高影响动作的授权。
- **Native Confirmation Record**：由 main-owned surface 生成并绑定 nonce、operation digest、user gesture、subject 和 expiry 的不可伪造确认。
- **Runtime Manifest**：受信 backend executable、入口、版本、hash 和发布身份。
- **Provider Grant**：对 provider/model/purpose/asset/payload 的明确远端处理授权。
- **Payload Manifest**：实际准备发送内容的类别、范围、字节和 hash。
- **Egress Artifact**：由受信边界生成、限制尺寸与格式的外发副本。
- **Audit Event**：不含敏感 payload 的 capability 生命周期与操作结果。

## Success Criteria

### Measurable Outcomes

- **SC-001**：全部 production IPC、HTTP、MCP 和 Bot actions 的 capability classification 覆盖率为 100%。
- **SC-002**：受信 renderer 提交任意 executable、命令字符串、目录、不可执行文件和 symlink 的进程启动次数均为 0。
- **SC-003**：`..`、绝对路径、root 外 symlink、中间 symlink、TOCTOU 和 poisoned response 测试中，root 外读取字节数为 0。
- **SC-004**：转码、decode、MIME 或尺寸验证失败时，外部 SDK 收到原始路径或原始字节的次数为 0。
- **SC-005**：无 grant、过期 grant、重复 grant、scope mismatch 和 hash mismatch 的 provider 网络发送数均为 0。
- **SC-006**：授权 provider 请求的 disclosure manifest 覆盖率为 100%，manifest 与实际发送字节范围一致。
- **SC-007**：持有 session token 的 compromised-surface 测试不能自助签发 grant、伪造或重放 native confirmation，也不能完成未授权 root/DB 切换、文件读取、高影响 mutation、egress 或 process-start。
- **SC-008**：Capability 撤销后 1 秒内不再开始新操作；所有已进入不可撤回阶段的操作都有明确审计状态。
- **SC-009**：正常 library 选择、本地索引、合法 runtime 启动、视频导出和批准聊天分享的完成率不低于当前 baseline。
- **SC-010**：安全边界增加的正常本地只读操作 p95 延迟开销低于 10%。
- **SC-011**：首次 discovery 的 100% 文件读取都可回溯到 native confirmation、root discovery capability 和 source manifest；后续读取接受 raw path 作为 authority 的次数为 0。
- **SC-012**：确定性 pre-network 失败的 grant 消费率为 0%；已经发送第一字节、timeout 或 response-lost 的请求自动重发率为 0%。

## Rollback and Degradation

- 安全失败不得降级为旧 raw path、原文件或宽松 Origin 行为。
- Runtime manifest 不可用时，系统保持 backend offline 并提供修复说明，不尝试任意解释器。
- Egress artifact 生成失败时，返回无附件结果或用户可重试状态。
- Provider grant 服务不可用时，远端处理停止；本地能力继续可用。
- 新 capability policy 上线需保留只读兼容窗口，但旧 mutation 客户端不得获得隐式豁免。

## Out of Scope

- 不试图防御已完全控制当前 OS 用户账户的攻击者。
- 不把 capability 系统作为 DRM 或跨用户权限系统。
- 不在本规范中选择签名技术、token 格式或 policy 框架。
- 不改变正常用户批准分享媒体的产品目标。
- 不把所有本机进程都假设为可信。

## Assumptions

- Electron main 和 backend policy owner 是能力签发与消费的可信边界；renderer 只请求具体操作，且无法取得签发材料。
- 用户确认通过 main-owned native surface 产生并绑定当前 operation；网页内容不能以调用 API 或提交字段自行伪造。
- 不可逆外发需要比本地只读更严格的授权和审计。
- 现有新媒体 FD 校验可作为行为基线，但实现技术可替换。
- 同一 OS 用户权限不等于所有应用 surface 权限相同。

## Dependencies

- Spec 004 提供 compromised-surface、network deny、symlink/TOCTOU 和 disclosure 验收工件。
- 007-A 使用当前 database UUID、approved-root manifest 与 source identity 完成已知能力收口，不依赖 Spec 008，也不等待完整 Spec 004 benchmark。
- 007-B 的最终 library/database/asset/operation contract 与 Spec 008-A/008-B 共同收敛；在此之前不得用依赖循环推迟 007-A。
- Photon、Codex 与未来集成必须消费同一个 capability policy contract。
