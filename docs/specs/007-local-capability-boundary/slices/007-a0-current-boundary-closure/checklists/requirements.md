# Requirements Quality Checklist: ML-007-A0

- [x] renderer、main 与 paired Agent authority 明确分层。
- [x] runtime admission、root/DB、source read 与 output publication 均有负向 oracle。
- [x] “默认不发原图”未扩写为“所有 provider 永不外发”。
- [x] plugin safe-default 与 opt-in local API 边界明确。
- [x] 未完成 capability census 和 provider ledger 明确保留。
- [x] 验证入口不访问私人 Library 或真实 provider。
