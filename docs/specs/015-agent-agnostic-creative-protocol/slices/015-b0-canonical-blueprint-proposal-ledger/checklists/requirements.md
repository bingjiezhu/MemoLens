# Requirements Quality Checklist: ML-015-B0

## Product Alignment

- [x] Blueprint成为项目真源，但未被伪装成用户定稿。
- [x] 每个有效修改有不可变记录，可通过新 revision恢复，不删除历史。
- [x] 一键先出提案与以后深度共创保持同一 revision链。
- [x] B0不把安装、模型、云、特效、渲染或素材文件操作混入项目内核。
- [x] B0/B1/B2职责明确，当前切片不以放松authority换取假闭环。

## Contract Precision

- [x] candidate与persisted schema的object、digest与version lifecycle分离。
- [x] canonical state、write authorization、provenance claim和semantic authority分离。
- [x] explicit head、initial adoption、CAS、no-op、restore和corrupt-head语义明确。
- [x] operation coverage明确为Blueprint-only，不声称完整project history。
- [x] durable receipt永久语义与现有24h transport cache分离。

## Safety

- [x] body不能自报actor/origin/authority/user-confirmed/persisted identity。
- [x] strict JSON、closed-world与资源ceiling在DB proof前执行。
- [x] replay在current head、receipt expiry、reference availability等易变检查之前。
- [x] DB proof、revision、head、operation、receipt与response在同一事务。
- [x] plugin继续只读，不从API trust或desktop token推导Agent写/用户确认。
- [x] 本切片无网络、模型、媒体、任意路径、shell、渲染或文件管理副作用。

## Migration / Integrity

- [x] V2/V3 migration tuple与checksum保持冻结。
- [x] future schema在任何DDL前fail closed；schema version只单调前进。
- [x] v3升级前有本地可验证backup；V4 additive且不自动backfill Blueprint。
- [x] immutable relation有数据库级UPDATE/DELETE阻断。
- [x] old v3 binary的正常schema downgrade被trigger阻断。
- [x] valid current只来自head→exact revision/digest/operation完整链，不使用MAX fallback。

## Implementation Gate

- [x] persisted schema/app/plugin parity与validator/compiler已实现并通过边界测试。
- [x] migration/backup/old-binary/future-schema/rollback测试通过。
- [x] commit/no-op/restore/CAS/durable replay/fault injection测试通过。
- [x] API auth/binding/strict JSON/authority/I-O测试通过。
- [x] CLI/MCP/project-open/read-history parity与corrupt-no-fallback测试通过。
- [x] 全量回归与独立复审通过，无未解决 P0/P1/P2。
