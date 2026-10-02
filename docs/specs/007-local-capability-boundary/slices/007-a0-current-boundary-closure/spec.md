# Feature Specification: Current Boundary Closure Baseline

- Feature ID：`ML-007-A0`
- 状态：`IMPLEMENTED / LOCALLY VALIDATED`
- 父规范：[ML-007](../../spec.md)
- 优先级：Immediate gate

## Objective

把当前已经存在的 runtime、root/DB、renderer/main、media read、render output 与 Agent authority 防线冻结为可执行回归门禁，并诚实列出尚未闭合的能力。

## Implemented Foundation

- Electron renderer 使用 sandbox、context isolation 和窄 IPC；main authority token 不进入 preload/renderer。
- backend 身份通过有界 challenge proof 验证后才注入 desktop token。
- Python runtime 固定为项目托管解释器并以 `-I` 启动；不接受 renderer 提供的任意 executable。
- Library DB 位于 app-state，并按所选 root 形成稳定 locator；media/output route 对 root、source、hash、token 和 idempotency 有合同测试。
- paired Agent capability 只允许短期、项目级、可恢复的 Blueprint proposal；MCP 保持只读。
- 默认 copy/retrieval 路径有“不发送原图”回归测试；这不等价于完整 provider payload ledger。

## Real Residuals

- 尚无完整 surface capability census 与机器可读 mutation manifest。
- provider payload/grant 的统一审计、撤销和 deletion closure 尚未完成。
- 首页 legacy 图片索引仍与 canonical media import 并行，最终 root/operation contract 依赖 ML-008-A 收敛。
- 本切片不证明受攻陷的同 OS 用户进程隔离，也不声明所有未来 adapter 已被覆盖。

## Requirements

- **FR-A0-001**：必须存在单一入口 `bash docs/specs/007-local-capability-boundary/slices/007-a0-current-boundary-closure/verify.sh`。
- **FR-A0-002**：入口必须覆盖 backend identity、credential separation、runtime admission、root/path/symlink、media mutation token、render publication、Agent authority 与默认无原图外发。
- **FR-A0-003**：测试只能使用 mock、临时目录或合成媒体；不得访问真实 provider 或私人 Library。
- **FR-A0-004**：任何未被测试覆盖的 provider/adapter 或 mutation surface 必须保持 `unverified`，不得由邻近测试推断为安全。
- **FR-A0-005**：边界测试失败时入口必须非零退出，且不能自动放宽 policy。

## Success Criteria

- 当前已列明的 boundary suite 同次运行全部通过。
- desktop/main/Agent 三种 authority 仍不可互换。
- 原图、任意路径、任意 runtime 和未批准写能力的负向测试保持 fail-closed。

## Local Validation Evidence

2026-08-23 使用隔离的 Python 3.12.13 / SQLite 3.53.1 运行 `verify.sh`：57 项 backend/media/strict-JSON/Agent 协议、22 项 plugin Wiki egress、49 项 Electron trust/artifact，共 128 项通过。测试未连接真实 provider 或私人 Library；因此不能提升未覆盖 adapter 的安全状态。
