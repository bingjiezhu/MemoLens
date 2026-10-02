# Tasks: ML-015-A1

- 状态：`IMPLEMENTED / VALIDATED`

- [x] T001 冻结切片范围：仅 blueprint-shadow + blueprint-validate，无 migration、写入、UI、模型或网络。
- [x] T002 冻结 candidate object/version、全部顶层字段、authority 语义、四轴状态与资源 ceiling。
- [x] T003 定义 legacy → candidate/v1 allowlist 映射、exact base binding、latest-no-fallback 与 projection gaps。
- [x] T004 加入并校验版本化 candidate/v1 JSON Schema 工件、package inclusion 与 status version/digest。
- [x] T005 完成 strict JSON CLI/MCP boundary：duplicate/non-finite/UTF-8/bytes/depth/nodes/container/string/direct-object。
- [x] T006 实现唯一 Creative Blueprint validator：closed-world schema、跨字段规则、canonical digest、64-error truncation。
- [x] T007 实现四轴 evaluator，确保 schema/base/reference/readiness 与 authority 互相独立。
- [x] T008 实现单快照 legacy shadow projector；损坏 latest 不回退，不返回 raw brief/candidate/provenance/path。
- [x] T009 在 ReadOnly Store 与 MemoLensGateway 暴露两个 Agent-neutral 方法，不增加数据库 relation/migration。
- [x] T010 增加 CLI `blueprint-shadow` / `blueprint-validate` 与 MCP `memolens_blueprint_shadow` / `memolens_blueprint_validate` 薄适配。
- [x] T011 增加 schema、projection、四轴、authority、corrupt/stale/conflict、CLI/MCP 等价专项测试。
- [x] T012 增加资源边界、错误不反射、隐私、单 snapshot、无网络/媒体/source 或持久状态写入对抗测试。
- [x] T013 更新 status/插件清单/README/版本记录，但不新增 schema dump tool，不删除 A0 `creative_blueprint_unavailable`。
- [x] T014 运行 plugin/backend/Node/renderer/typecheck/lint/compile/JSON/diff 全量验证。
- [x] T015 完成独立产品合同与安全复审；无 P0/P1/P2 后将 spec 标为 `IMPLEMENTED / VALIDATED`。
