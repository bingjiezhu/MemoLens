# Implementation Plan: ML-015-B1

- 规范：[spec.md](spec.md)
- 状态：`COMPLETE`
- 原则：先冻结合同与负面边界，再以最小垂直闭环接通 Core → Backend → Electron main → CLI；B2 能力保持阻断

## Architecture

```text
CLI credential store (0600)
  ↕ pairing / nonce / HMAC proof
Backend in-memory broker
  ↕ main-only authority token
Electron main + native dialog
  ↕ definitive BEGIN IMMEDIATE
V5 capability events + Agent receipt
  → existing B0 Blueprint command handler

Electron main native decision review
  → V5 authority event/head/receipt
  → derived authority projection
  → Backend / plugin read parity
```

## Phases

### Phase 1 — Contract and migration

- Freeze endpoint, HMAC, actor/origin, error, authority and carry-forward contracts.
- Add V5 additive schema, v4 backup, collision/physical verification and immutable triggers.
- Preserve V1–V4 statements/checksums byte-for-byte.

### Phase 2 — Pairing and paired command context

- Add per-launch main authority token/runtime epoch.
- Add bounded pending/nonce broker and project-scoped capability service.
- Extend Blueprint command context, operation actor union and cross-receipt validation.
- Make capability use + B0 command mutation one transaction.

### Phase 3 — Decision authority

- Add presentation, confirm/revoke commands and permanent authority receipts.
- Implement direct-parent digest continuity and restore non-resurrection.
- Overlay projection on current/exact Blueprint reads without changing persisted Blueprint/v1.

### Phase 4 — Product surfaces

- Add main-owned pairing/revoke/authority IPC and native dialogs.
- Add minimal renderer panel for pending pairing, active capabilities and 8-unit review.
- Add CLI pairing/status/commit/restore with restricted credential storage.
- Keep MCP write disabled; update capability discovery honestly.

### Phase 5 — Verification

- Migration, corruption, actor, receipt, CAS, replay, race and fault-injection tests.
- Renderer-compromise/main-token leakage and native presentation tests.
- CLI no-proxy/loopback/secret-output/credential-permission tests.
- Full `npm run check`, focused randomized state machine, independent architecture and security review.

## Key Decisions

- V5 uses additive Agent receipt table instead of rebuilding B0 desktop receipt bytes.
- pending pairing and nonce are bounded runtime memory; durable grant facts/events contain only secret hash.
- CLI is the B1 write surface; MCP remains read-only until a separately reviewed in-memory session transport exists.
- persisted Blueprint/v1 remains frozen/unverified; authority is a separate append-only projection.
- projection continuity is direct-parent digest based; restoring equal historical content does not jump a broken chain.
- pairing has short TTL/max operations and no Timeline/render/export/file scope.
- Authority JSON 使用跨 Core/HTTP/Electron/plugin 的共同预算：逐值 1 MiB、每项目 4096 events、event JSON 与 receipt response JSON 各 64 MiB 聚合上限；合法 near-limit state 不能被下游表面用更窄限制拒绝。
- Core 与 standalone plugin 使用 revision merge-stream 和 event/receipt join-stream；投影只保存有界 confirmation window/cache，不构建 `revision × 8 units` 全量 digest 图或 N+1 查询。
- 已完成 receipt replay 在 prospective capacity gate 之前识别；真正的新写超限时使用稳定 409，且事务内没有部分 event/head/receipt。
- Native capability 列表只投影 current epoch 的 active grant；历史 capability 留在不可变安全账本，不进入当前操作面。

## Rollback / Degradation

- V5 migration failure leaves the v4 source transaction uncommitted and retains the verified backup.
- main authority token unavailable：pair/revoke/confirm disabled; B0 desktop proposal and all read-only features continue.
- broker restart：all pairing sessions require re-pair; durable prior project/authority history remains readable.
- authority ledger corruption：authority projection fails closed; Blueprint semantic bytes are not rewritten or silently downgraded to confirmed.
- Agent CLI credential unavailable：Agent falls back to read-only and prints a pairing recovery action without exposing secrets.
- 历史 capability 验证保持 tamper-evident，不为降低查询数而只读 current epoch。当前 action surface 已与历史列表解耦；后续 V6/维护切片再冻结项目级 capability admission/aggregate budget 或验证 checkpoint，以解决长期 `O(capabilities + Agent operations)` 可用性，而不伪造“历史已验证”。
