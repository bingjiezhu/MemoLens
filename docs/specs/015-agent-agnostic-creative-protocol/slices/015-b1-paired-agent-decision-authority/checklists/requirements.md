# Requirements Quality Checklist: ML-015-B1

## Product alignment

- [x] 一次 pairing 后普通可撤销项目写不逐次弹窗。
- [x] pairing approval 与 semantic user confirmation完全分离。
- [x] Agent可提交 proposal/restore，但永远不能代替用户确认。
- [x] 8个decision unit沿用B0，不新增第二套粒度。
- [x] 8/8 confirmed不冒充可发布或可渲染。
- [x] restore不复活历史authority。
- [x] B2 compiler/workbench/history/render/export明确排除。

## Security and integrity

- [x] main authority token与desktop token分离且不暴露renderer。
- [x] capability绑定DB/runtime/project/subject/scope/TTL/max-use。
- [x] 每个Agent command使用single-use nonce和exact request HMAC。
- [x] definitive authorization/use与domain mutation同事务。
- [x]旧desktop actor/receipt逐字兼容；Agent actor/receipt诚实且封闭。
- [x] pending/nonce只在有界内存，DB不保存raw secret。
- [x] CLI secret不进入普通输出；credential文件权限与残余风险明确。
- [x] authority event/head/receipt append-only、CAS、永久replay、fail closed。
- [x] changed-unit失效、direct-parent carry与restore边界可机械测试。

## Testability

- [x] 所有故事都有独立测试路径。
- [x] pairing/proof/authority有稳定错误码。
- [x] 成功标准覆盖未授权写、弹窗数量、replay、atomicity、随机状态机、迁移与泄漏。
- [x] threat model明确列出TCB与不承诺的同OS用户攻击。
- [x] rollback/degradation不回退到raw SQL、direct SQLite write或宽松token。

## Review result

- [x] 无待澄清产品问题。
- [x] 无把B1实现隐式扩成B2的任务。
- [x] 已实现并通过仓库级门禁与独立复核；P0/P1 为 0，非阻断 P2 已在 spec 残余风险中显式记录。
