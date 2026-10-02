# Requirements Quality Checklist: ML-015-A0

## Scope and Product Alignment

- [x] 直接服务“人负责思想，Agent 负责执行”与跨 Agent 继续同一项目的共识。
- [x] 将结构化项目状态而非聊天 transcript 定义为恢复依据。
- [x] 只交付当前 schema 能可验证地支持的读能力。
- [x] 不将 legacy brief 伪称为 Creative Blueprint。

## Honesty and Evidence

- [x] Timeline project head 明确是 latest observed projection，不是 authoritative head。
- [x] digest/JSON/binding 损坏不静默回退。
- [x] Evidence 只返回受限 ID/URI，可由 Wiki 进一步解引用。
- [x] 不宣称现有 provenance 是完整 operation ledger。
- [x] 所有未实现的 Blueprint/head/write/history/Wiki pinning 能力均有明确 gap。

## Safety and Compatibility

- [x] 不增加数据库 migration，仅扩展固定读 relation allowlist。
- [x] 单次响应只使用一个私有 SQLite snapshot。
- [x] 无网络、无 Library 扫描、无原媒体打开、无 DB/文件写入。
- [x] 不返回 raw brief/timeline/provenance、绝对路径、provider/cache 或 data URL。
- [x] canonical `timelines` 与 compatibility `timeline_revisions` 优先级固定，不合并真源。
- [x] 旧 timeline-list/get 对外合同保持不变。

## Ready for Implementation

- [x] 用户已授权按小切片实现 Grill Me 共识。
- [x] 功能边界、非目标、降级与回滚路径明确。
- [x] 每个用户故事可独立验收。
- [x] 成功指标可以自动化测试。
- [x] 实现不需要新的用户选择或高影响授权。
