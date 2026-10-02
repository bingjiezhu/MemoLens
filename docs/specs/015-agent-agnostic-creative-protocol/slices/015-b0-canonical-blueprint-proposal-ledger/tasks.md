# Tasks: ML-015-B0

- 状态：`COMPLETE / VALIDATED`

- [x] T001 完成产品、持久化与安全三路只读审查，冻结 B0/B1/B2 边界。
- [x] T002 冻结 persisted Blueprint、typed command、explicit head、operation与durable receipt合同。
- [x] T003 新增 persisted Blueprint/v1 schema工件、app/plugin exact-byte parity与纯 validator/compiler。
- [x] T004 加固 schema preflight，新增 v3 backup、V4 migration、immutable/downgrade triggers与checksum测试。
- [x] T005 实现 repository exact reads、durable replay、CAS commit/no-op/restore全原子原语。
- [x] T006 实现 BlueprintService 与 strict HTTP JSON gate；actor/origin/authority全部server-derived。
- [x] T007 新增 desktop-authenticated commit/restore与loopback read/history API。
- [x] T008 在 canonical Blueprint 存在或账本损坏时 fail closed；阻断 legacy Timeline 新建、revision、新 render，并保留旧 read/冻结 replay。
- [x] T009 实现 plugin persisted Blueprint reader、current/exact/history presenter与single-snapshot fail-closed验证。
- [x] T010 增加 CLI/MCP `blueprint-get/history`；保持 `write_blueprint=false`。
- [x] T011 更新 project-open/status/gaps，确保 valid head优先、corrupt head不回退、shadow语义不变。
- [x] T012 完成 schema/authority/strict JSON/CAS/idempotency/concurrency/fault/privacy/I/O专项测试。
- [x] T013 运行 plugin/backend/Node/renderer/typecheck/lint/compile/JSON/diff全量验证。
- [x] T014 完成独立产品/架构与安全复审；无未解决 P0/P1/P2，并更新规范与变更记录。
