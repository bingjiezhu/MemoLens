# Implementation Plan: ML-015-B2B2

## Architecture

1. **V9 persistence**：recoverable V8 backup；same-name rebuild Timeline operations/receipts 以接纳第二个 command type；revisions/heads 和 V8 Export tables 原样保留。
2. **Core command**：新增 `timeline.reconcile_from_coverage/v1`，严格 expected head、parent、`N+1`、CAS、receipt replay。
3. **Cold audit**：从 initial revision 到 current head 逐 revision 重放 Blueprint/Coverage/lowerer/source binding，并验证完整链。
4. **Service/API/UI**：Desktop-only reconcile POST；workspace 只在 exact stale、zero-gap、source-current 条件下显示动作。
5. **Inspection**：纯 renderer model 建立 exact clip/media plan；React 只读播放，不创建后端 effect。

## Verification gates

- Gate 1：fresh V9、V8→V9、collision/rollback/backup/foreign-key/digest preservation。
- Gate 2：revision `N+1`、parent、history、same-key replay、stale/current/concurrent/fault/tamper。
- Gate 3：API authentication/closed body/stable 409；Renderer exact adoption/reason parity。
- Gate 4：inspection half-open mapping、same-origin URL、identity reset、build/typecheck。
- Gate 5：Coverage+Timeline strict lane 与最终 `npm run check`；证据按实际结果填写。

## Rollback

关闭 reconcile POST 和 UI capability，保留 V9 ledger 可读审计；不得 destructive down-migrate。inspection 是无持久化 renderer surface，可独立关闭，不影响 Timeline/Export 真源。
