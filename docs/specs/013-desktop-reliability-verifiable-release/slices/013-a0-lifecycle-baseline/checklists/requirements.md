# Requirements Quality Checklist: ML-013-A0

- [x] backend process owner 与整个 Electron app singleton 未混为一谈。
- [x] source-tree test 与正式安装包验证未混为一谈。
- [x] hidden ancestor、document background、cleanup 与 resume 均进入 polling contract。
- [x] 旧 writer 未确认退出时 fail-closed。
- [x] clean VM、签名、公证、N-1 migration 保持明确缺口。
- [x] 验证入口不修改用户 Library 或正式 app-state。
