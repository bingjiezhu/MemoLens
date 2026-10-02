# Implementation Plan: ML-017-A2

## Architecture

1. **Core trusted read**：把 canonical Usage search reader拆为 transaction-capable helper；五表 UNION project roots 后逐一 cold audit。
2. **Projection**：纯 Python half-open union/residual；asset/candidate/source-duration 双层验证；Search revision 固定 validated occurrences。
3. **Presentation**：Backend 输出 closed 11 字段 Usage；Renderer 严格 adoption，不做 silent coercion。
4. **Admission**：UI 提交 policy/revision/9+11 exact candidates；Director 在 idempotent immediate transaction 内重读、重算、比较并写入 provenance。
5. **Recovery UX**：native export submitted/unknown 后 bounded canonical polling；终态通知 workspace，保留 manual Refresh。

## Verification gates

- 750 randomized interval fixtures + malformed/source-boundary tests。
- occurrence deletion与 operation+occurrence double-delete tamper。
- strict response fixture matrix、candidate/policy switching、partial-used UI guard。
- fresh/revision-changed/head-changed/used-after-search Director admission 与 zero partial writes。
- export polling exact-job/unknown/terminal/bounds/cancellation model tests。
- final `npm run check` 与真实 evidence 分层记录。

## Rollback

可关闭 Usage search filters、usage-aware brief create 和 automatic polling，同时保留 immutable Export/Usage ledger。不得删除 Usage rows或回写一个布尔 used flag；已有 brief provenance 保持可读。
