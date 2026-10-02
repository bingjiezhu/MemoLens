# Implementation Plan: ML-015-B2B Deterministic Timeline Lowering

- 状态：`IMPLEMENTED IN SHARED WORKTREE / LOCAL VALIDATION WITH RESIDUALS`
- 规范：[spec.md](spec.md)
- 原则：Coverage Plan 是唯一选材真源；B2B 只 lowering，不新增第二个 planner。

## 1. Architecture decision

```text
Desktop materialize request
  → expected database UUID + explicitly null Timeline head
  → BEGIN IMMEDIATE
  → validate trusted current Blueprint ledger
  → validate trusted current Coverage ledger
  → require exact Coverage→Blueprint binding, A1 baseline replay and zero gaps
  → resolve one fixed current source per selected assignment
  → pure hard-cut lowerer
       stable Timeline/track/clip IDs + separate source-manifest digest
       one Beat → one clip
       no transitions/subtitles/audio program/replanning
  → pure canonical Timeline validator
  → atomically commit revision 1 + operation + empty→revision-1 head + receipt
  → commit

Workspace read
  → validate Blueprint + Coverage + Timeline ledgers in one read boundary
  → derive upstream/source freshness and capabilities
  → inspect exact Beat→clip mapping
  → B2B grants no preview/approval/export authority
```

Canonical Timeline document 自身包含 `revision=1`、`parent=null` 和 exact upstream operation bindings，但不携带 own operation ID、created-at、本地路径或 execution-local `asset_source_id`。持久层保存该 closed document 的 canonical bytes/SHA-256，并将按 clip 排列的 source-binding manifest 及其 digest 分开保存。因此只改变 source locator identity 不会污染 Timeline content digest，但会如实改变 source-bindings digest。

## 2. Data contract

### Canonical Timeline document v1

Closed 顶层字段：

- `object/schema_version/project_id/timeline_id/revision/parent`
- exact `blueprint_binding`
- exact `coverage_binding`
- `compiler`
- `output={duration_ms,aspect_ratio}`
- one `primary_visual` track

Clip 保存稳定 ID、Beat/assignment/evidence/asset identity、kind、Timeline 起止、`fit=cover`、`audio_enabled=false` 以及 video source/analysis lineage。独立 source-binding manifest 一 clip 一行，包含 opaque `asset_source_id`；两者都不复制 script text、material reason、Coverage alternatives 或任何路径。

### Mechanical lowering rules

1. 重新验证 Coverage contract 并从 exact Blueprint 重放 A1 deterministic baseline；要求 Beat timing 从 0 开始、连续、无重叠，每 Beat 恰好一个 selected assignment 且无 Gap。
2. 按 Beat ordinal 创建 clip，`start_ms=beat.start_ms`、`end_ms=beat.end_ms`。
3. video 使用 proof span 的左边界与 Beat duration；image 使用 Beat duration 且不写 source range。
4. source 从同一交易快照解析：唯一 preferred available source 优先，否则 opaque source ID 升序。多个 preferred 是仓库不一致，fail closed。
5. `output` 从 Coverage output binding 只导出 exact duration 与受支持的 `16:9/9:16/1:1/4:5` aspect ratio；B2B 不定义 geometry、fps、sample rate 或 background。
6. compiler 固定 `memolens.timeline-lowerer/v1`、`transitions=hard_cut`、`audio=silent`、`subtitles=none`；clip 固定 `fit=cover/audio_enabled=false`，不读 Technique/reference 也不生成 transition/audio/text track。

## 3. V7 persistence

Additive migration 新增：

- `canonical_timeline_operations`
- `canonical_timeline_revisions`
- `canonical_timeline_heads`
- `canonical_timeline_receipts`
- project/sequence/digest/CAS 所需 index
- operation/revision/receipt immutable triggers 和 head protected-update trigger

必须将四张表纳入 physical schema manifest、protected immutable-prefix/trusted-floor 和 transaction-local attestation。trusted read 必须验证可见的 initial operation↔revision-1↔head↔receipt 一致性、canonical bytes/digests、exact upstream binding 和 result identity。V7 schema 有 forward-only head guard，但当前 B2B service 只接受 empty head 并创建 revision 1；不声称已交付 successor/history/manual-edit command。

V7 不改任何 legacy `timelines` row。新表发生 collision、trigger/index/view 同名、V1–V6 checksum drift 或物理 schema 不匹配时，启动必须在第一次领域写之前失败。升级前生成受限 schema backup；recovery 不删除或降级用户媒体。

## 4. Components

- `core/timeline_lowering_contract.py`：pure closed contract、stable IDs/digest、zero-gap hard-cut lowerer 和 validator。
- `core/media_db.py`：V7 migration/manifest、source-binding resolver、trusted Timeline ledger read/write/CAS/receipt。
- `backend/src/media/timeline_lowering.py`：materialize/read/freshness service，不复用 legacy planner。
- `backend/src/media/blueprint.py`：在 canonical workspace 同一读边界中加入 Timeline summary，不把 legacy Timeline 提升为 current。
- `backend/src/api/routes.py` / `backend/src/__init__.py`：Desktop-authenticated materialize、read resource 与 production wiring。
- `src/blueprint/timelineTypes.ts`：canonical Timeline closed renderer types。
- `src/blueprint/timelineModel.ts`：strict normalization、identity/freshness/capability adoption guard。
- `src/blueprint/timelineApi.ts`：path-free GET 与 Desktop-authenticated materialize client。
- `src/blueprint/BlueprintProjectWorkspace.tsx`：materialize/refresh、Beat→clip inspect、draft/source state，以及 preview/approval/export authority 的明确分离。
- `tests/test_timeline_lowering_contract.py`：determinism、closed schema、zero-gap 与 no-replanning 纯测试。
- `tests/test_timeline_persistence.py`、`test_timeline_lowering_service.py`、`test_timeline_lowering_api.py`、`test_timeline_lowering_integration.py`：migration/initial-head CAS/idempotency/fault/tamper/source freshness/API 测试。
- renderer model/API/session 测试：身份采用、stale response、existing-head-wins、honest capability 和 preview/approval/export authority separation。

## 5. Phases and gates

### Phase 0 — Baseline lock

1. 记录 branch/HEAD/package/runtime/SQLite/worktree state，保留其他 agent 与用户修改。
2. 冻结 A1 final implementation evidence、V1–V6 migration/checksum/normalized schema、current V8 schema 中的 exact V7 Timeline catalogue 和 legacy Timeline guard baseline。
3. 先新增 red tests：Blueprint workspace 仍 `not_compiled`、zero-gap Coverage 无 canonical Timeline materialize 路径。

**Gate P0**：基线结果与 red failure 已保存，环境阻断与代码失败分开。

### Phase 1 — Pure contract

1. 冻结 closed document/source-manifest/limits/stable ID/content digest/compiler contract。
2. 实现 exact Beat→clip 与 source-binding input 的纯 lowerer。
3. 实现独立 validator，重算所有可导出字段，不信任 compiler 自己的输出。
4. 覆盖 Gap、multi-selected、时间不连续、span 过短、path/unknown field、ID/digest 篡改。

**Gate P1**：两个干净运行中同一 exact input 的 Timeline document/IDs/digest exact-equal，只更换 opaque source ID 时只有 source-manifest digest 变化；不调用 DB、网络或模型。

### Phase 2 — V7 ledger and command

1. 实现 V7 migration、collision/preflight/backup/recovery/physical schema verification。
2. 在 transaction-local trusted snapshot 中复用 Blueprint 和 Coverage 完整审计结果，不跨事务 cache。
3. 实现 deterministic fixed source resolver 和 no-path binding manifest。
4. 实现 empty-head→revision-1 的 operation/revision/head/receipt atomic command、same-key replay 和 existing-head guard。
5. 实现 current/exact-revision/freshness trusted read，损坏时 stable 409，不 fallback；history-list 和 successor revision 不在本切片。

**Gate P2**：current-schema fresh DB 含 exact V7 catalogue、V6→current 经 V7/V8 升级与 pre-V7 backup、tamper、rollback、fault injection、100-round concurrent first-cut 和 resource lifecycle 通过；V1–V6 exact oracle 不变。

### Phase 3 — API/workspace

1. 接入 production runtime，GET read 与 Desktop POST 使用 stable error/identity preconditions。
2. 将 Timeline summary 接入 canonical workspace：Coverage 尚未 lowerable 时保留 honest empty state；有 draft 时显示 exact Timeline head/freshness。
3. 保留 Blueprint project 的 legacy Timeline create/revise/render 硬阻断和 B2A history role；B2B 不开放 preview/export root/profile。

**Gate P3**：API 和 production `create_app` 测试证明只有 exact current zero-gap state 能 materialize，legacy 路径仍关闭，B2B 不把 draft 推导为 preview/approval/export authority。

### Phase 4 — Renderer UX

1. 添加 closed TypeScript contract/normalizer/API，任何 database/project/Blueprint/Coverage/Timeline head 不匹配都拒绝 adoption。
2. 增加 explicit materialize/refresh，不在 resume/Coverage refresh 时自动编译。
3. 显示 Timeline revision/content digest、Beat→clip、opaque source status、hard-cut 限制、draft/not-approved/not-exportable；不默认展示 raw JSON，也不声称卡片已展示全部 upstream digests。
4. stale/conflict/integrity 后只经 Core refetch 恢复，不本地伪造 current；B2B UI 不启用 preview。
5. 完成 desktop/mobile、keyboard/focus/ARIA/touch target 的 Electron materialize/inspect 视觉验收。

**Gate P4**：真实 renderer/Electron 证据覆盖 missing/not-lowerable/current/stale-source/conflict/integrity，并证明 preview/approval/export 不会被 B2B 误升级。

### Phase 5 — Regression and evidence

1. 重跑 A1 Coverage、B0/B1/B1A/B2A trusted ledger、legacy Timeline/render guard 和 plugin safe-reader 回归。
2. 运行 256-Beat performance profile、full fault/tamper/privacy matrix 与 100-round empty-head concurrency/replay。
3. 在 final diff 上运行 `npm run check`，记录 exact counts/runtime/base commit 和 residuals。
4. 独立复审真源、hidden planner、existing-head-wins、stale source、preview/export 权限和 legacy fallback。
5. 只将实际观测结果写入新的 `implementation-evidence.md`，再更新 Spec/Tasks 状态。

**Gate P5**：当前 checkout 的所有 P0/P1 问题关闭，本地/Remote/Desktop 证据边界按实际状态记录。

## 6. Test matrix

| Domain | Positive oracle | Failure/adversarial oracle |
| --- | --- | --- |
| Pure lowering | exact zero-gap Coverage → 1:1 hard-cut clips | Gap/multi-selected/reorder/drop/extra/transition/subtitle rejected |
| Source binding | deterministic available source fixed path-free | multiple preferred, missing/changed/revoked, forged source, implicit failover rejected |
| Determinism | exact inputs replay has exact Timeline bytes/IDs/digest; relocated opaque source leaves Timeline unchanged | UUID/time/rowid/path/compiler self-claim mutation detected; source-manifest digest still binds actual source |
| Ledger | atomic append + CAS head + permanent receipt | operation/revision/head/receipt/parent/result tamper and partial fault fail closed |
| Upstream freshness | exact current Blueprint+Coverage accepted | stale Blueprint/Coverage/evidence or mixed snapshot rejected |
| Existing head | exact empty head creates only revision 1 | any existing or concurrently won head always wins; no successor/auto retry |
| Downstream authority | B2B exposes path-free exact current/source-current draft only | preview/export root/profile, stale source/revision/digest and legacy Timeline authority are not granted by B2B |
| UI | materialize/inspect honest states | stale response adoption, auto materialize, preview/approved/exportable inflation rejected |
| Migration | current-schema fresh DB contains exact V7 catalogue; V6→current crosses V7/V8 with pre-V7 backup | collision/checksum/schema drift/rollback/tamper detected |
| Regression | A1/B0/B1/B1A/B2A/legacy-only remain green | legacy Timeline promoted or new MCP/file/model capability detected |

## 7. Rollout and rollback

- V7 是 additive project-state migration，不修改原媒体或 legacy Timeline。发布前必须冻结 backend/renderer 同版本边界。
- materialize 是用户明确触发的 `reversible_project_write`；B2B 不创建 preview/render artifact，也不自动运行。
- 若 lowering/read 出现 integrity 风险，回滚应关闭 materialize capability 并保留 V7 ledger 可读审计；不删除 revision、不回拨 head、不恢复 legacy Timeline 为 current。
- 不做 destructive down-migration。需要回退版本时，旧应用必须对 schema version 7 fail closed，不忽略新表继续写 V6。
