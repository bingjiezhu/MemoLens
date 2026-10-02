# Specification Quality Checklist: ML-017-A2

- [x] immutable fact 与可重建 projection 分开。
- [x] absence/tamper、source boundary 和 TOCTOU authority 有 adversarial oracle。
- [x] search explanation 与 brief admission 分开，Renderer 不被当作 authority。
- [x] partial-used residual 的可见与不可安全选入边界明确。
- [x] legacy compatibility、rollback、deferred parent scope 和 promotion rule 明确。
