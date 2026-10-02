# Requirements Quality Checklist: ML-014-A0

## Scope and Product Alignment

- [x] 直接服务“一个素材文件夹 → Agent 可搜索可剪辑”的共识。
- [x] 先提升素材理解的导航与证据定位，不把特效或补拍当首个阻塞项。
- [x] 外部 Agent 通过中性 CLI/Core contract 读取，MCP 只是适配层。
- [x] 原件不移动、不删除、不上传。

## Honesty and Evidence

- [x] live projection 与 materialized generation 明确分开。
- [x] 未分析、未固定 generation 和无素材不会混成同一结论。
- [x] 视频结果能回到精确 `[start_ms,end_ms)` 与 current successful analysis head。
- [x] deterministic facts 与 model hypothesis 分级。
- [x] 不宣称本切片提升了语义检索准确率。

## Safety and Compatibility

- [x] 不增加数据库迁移，不影响已冻结 V2/V3 checksum。
- [x] safe-default 无网络、无写入、无文件夹扫描。
- [x] 页面与证据不返回绝对路径、cache key 或原始字节。
- [x] 不可信文本和插件控制数据有明确边界。
- [x] 旧数据库和原有 15 个 MCP 工具无回退；新增 5 个后总数为 20，已做实际 payload/schema 回归。

## Ready for Implementation

- [x] 用户已授权实施。
- [x] 功能边界、非目标和回滚路径明确。
- [x] 每个用户故事可独立验收。
- [x] 成功指标可自动测试。
