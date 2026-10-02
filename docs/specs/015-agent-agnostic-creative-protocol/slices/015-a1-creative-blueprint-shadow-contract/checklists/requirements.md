# Requirements Quality Checklist: ML-015-A1

## Scope and Product Alignment

- [x] 直接服务“对话编译为 Creative Blueprint、聊天不是项目真源”的产品共识。
- [x] 同时支持一句意图、现成文稿、参考、指定素材或旧项目作为起点，而不把缺字段等同于 schema failure。
- [x] 保持“人负责意义与取舍，AI 负责建议与执行”：Agent 可提案但不能自证立场已获用户确认。
- [x] 只交付 shadow 与 validation，不提前实现 ML-015-B 写链。
- [x] CLI/Core 为 Agent-neutral 合同，MCP 只是薄适配。

## Contract Precision

- [x] candidate object=`memolens.creative_blueprint_candidate`、schema version=`1` 和全部 required 顶层字段已冻结。
- [x] nested fields、closed-world、nullable/empty 与跨字段语义有明确边界。
- [x] schema artifact version/digest 通过现有 status 发现，不增加第三个 schema tool。
- [x] shadow/validation response object 与四轴准确名称/枚举已冻结。
- [x] schema validity、binding、reference resolution、planning readiness 和 authority 不被合并成一个分数。
- [x] canonical digest 明确不是 signature、approval、authority 或 persisted revision。

## Authority and Projection Honesty

- [x] declared source 只允许 caller-declared input、agent proposal、legacy unknown 或 unknown。
- [x] `caller_declared_user_input` 不等于 `user_confirmed`，输出固定 `authority_verified=false`。
- [x] legacy brief 只按 allowlist 投影；stance/script/reference/technique/confirmation 不从自由文本猜测。
- [x] shadow 明确 non-authoritative、non-persisted、legacy projection。
- [x] latest 损坏不回退，observed Timeline 不伪称 authoritative project head。
- [x] A0 `creative_blueprint_unavailable` 在真正持久化 Blueprint 前继续保留。

## Safety and Resource Boundaries

- [x] candidate JSON 的 bytes/depth/nodes/object/array/string ceiling 均有明确数值。
- [x] MCP outer frame 与 nested candidate 使用两层 ceiling，tool schema 不作为执行期安全证明。
- [x] duplicate key、NaN/Infinity、invalid UTF-8、direct object cycle/type、bool-as-int 和 closed-world failure 均 fail closed。
- [x] errors 最多 64、确定性截断、固定模板且不反射输入值/未知 key/locator/path。
- [x] 单次调用最多一个私有 SQLite snapshot；无网络、模型、Library scan、原媒体 open 或写入。
- [x] validate 不回显 raw candidate；shadow 不泄漏 raw brief/candidate/provenance/provider/path/data URL。

## Testability and Degradation

- [x] 四个用户故事均有不依赖后续写链的 independent test。
- [x] 每个四轴枚举、authority 组合、corrupt/stale/conflict/no-ref/old-schema 都可构造 fixture。
- [x] 资源 ceiling 要求 limit-1/limit/limit+1 与 direct MCP object 对抗测试。
- [x] single snapshot、zero side effects、transport equivalence 和 recursive privacy scan 可自动化验证。
- [x] schema artifact 缺失/digest 不符、旧数据库和无 resolver 的降级行为明确。
- [x] 回滚无需 migration 或数据恢复，现有 A0/Wiki/Timeline 读能力保持不变。

## Implementation Gate

- [x] candidate/v1 schema artifact、validator、projector 与两个入口已实现并通过专项测试。
- [x] status version/digest 与部署包内 exact schema bytes 已验证一致。
- [x] plugin/backend/Node/renderer/typecheck/lint/compile/JSON/diff 全量回归通过。
- [x] 独立合同/安全复审确认无 P0/P1/P2。
- [x] 以上全部完成后，spec 已从 `IMPLEMENTATION IN PROGRESS` 更新为 `IMPLEMENTED / VALIDATED`。
