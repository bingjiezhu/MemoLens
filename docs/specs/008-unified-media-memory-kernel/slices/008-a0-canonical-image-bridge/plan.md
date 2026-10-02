# Implementation Plan: ML-008-A0.1 Canonical Image Publication + Shadow Projection

## Status and implementation rule

- 当前状态：`IMPLEMENTED / CONTROLLED-LOCAL GATES PASSED / PROMOTION BLOCKED`
- 纵向单图链、durable job、immutable publish、canonical-ID shadow、legacy backfill、projection rebuild/removal、V17 consumer read cutover、Atlas verified admission 与 Codex/DeepSeek canonical editor 已进入 production path。
- Phase 1–8 的 controlled-local implementation/fault/restart/replay/parity 与 final same-diff gate 已收口。Phase 0 的 historical RED baseline 仍缺失，不允许用现在的 green 倒推补造；后续只能补真实历史/实机 promotion 证据，不能改写已冻结的 controlled-local 边界。

## Architecture decision

采用 canonical-first、projection-later 的单向发布模型：

```text
enqueue exact image request
  → durable media_jobs(kind=image_analysis)
  → checkpointed analysis attempt
  → BEGIN IMMEDIATE
       append immutable image result
       CAS exact analysis head
       append projection change
       append publish receipt
       persist job publish checkpoint
    COMMIT
  → idempotent shadow projector
  → image_index canonical-ID row + alias delta + receipt (one transaction)
  → deterministic parity manifest
  → consumer generation switch
```

`image_index` 只能是兼容 projection；canonical revision 永远不从 projection 反向恢复。Canonical publish 成功而 projection 暂时失败时，canonical head 保持有效，job/reader 显示 `projection_pending`，projector 从 durable change position 重试。

## Logical data additions

在实施时使用当时的 next additive migration version；不要与并行切片争抢硬编码版本。逻辑上需要：

- **Image Analysis Result**：一条 analysis run 对应一个 versioned closed document 与 content digest；发布后 immutable。
- **Image Projection Change**：与 head CAS 同事务追加的 monotonic outbox/change record。
- **Image Publish Receipt**：固定 job、request、revision、head 和 change position，支持 exactly-once recovery。
- **Legacy Image Alias**：`legacy_id → canonical asset_id`，database-scoped、append-only、不可重绑定。
- **Image Projection Receipt**：固定 exact head/source binding 与 projected row/alias digests。
- **Image Projection Manifest / Generation**：固定 high-water mark、compiler version、完整 row-set/alias-set parity 与 active pointer。

可以复用 `analysis_runs` 的 revision number 与 `asset_analysis_heads`，但必须补齐 published result immutability、digest binding、CAS 和 triggers。Lifecycle status 与不可变 published result 不得混成可原地覆盖的一行 JSON。

## Closed contracts

### `image_analysis` job request

请求只接受 canonical identifiers 和 expected bindings：

- database UUID、runtime generation / database file identity；
- asset ID、source ID、root capability binding；
- input asset SHA 与 observed source identity；
- analysis profile/version；
- idempotency key；
- expected analysis head（首次分析为 explicit missing）。

客户端 raw absolute path、legacy `img_*` ID 和自由 worker/stage name 不得构成 authority。Legacy ID 必须先在同 database scope 内解析为 canonical ID。

### Image Analysis Result v1

结果文档至少固定：

- object/schema、asset ID/SHA、revision/run ID；
- exact source/input binding；
- analysis profile、producer/model/rule provenance；
- metadata/geocode/vision/embedding/quality 的 typed stage outcomes；
-可投影字段和 artifact digests；
- canonical content SHA。

A0.1 只迁移当前真实输出，不把 text-derived embedding 重标为 visual evidence。未知、disabled、partial 与 failed 必须保留为显式 outcome。

### Deterministic shadow renderer

Renderer 输入只允许 exact current Image Analysis Result、admitted source projection 与 projector version，输出 legacy-compatible row。Canonical digest 排除 wall-clock timestamp；展示时间可以由 envelope 提供。每个 row 的 `id` 必须等于 canonical asset ID。

## Component changes

- `core/media_db.py`
  - next additive migration、immutable result/change/receipt/alias/manifest stores；
  - image analysis job creation、exact DB/source binding、head CAS、idempotency；
  - immutable/no-rebind triggers 与 strict verified reads。
- `backend/src/media/import_plan.py`
  - image import 也创建 canonical `image_analysis` job；同一 import request 内固定 asset/source/job identity。
- `backend/src/media/`
  - 增加 image analysis command/worker/projector；把 worker registry 从 video-only 收敛为 closed media registry。
- `indexing/pipeline.py`
  - production persistent path 改为 enqueue/compatibility adapter；移除同步 full analysis 和 legacy-first write。
- `core/db.py`
  - 直接 mutable image upsert 限制为 projector/migration owner；普通 route 无法旁路 canonical publish。
- `backend/src/media/mixed_presenter.py` 与 current image read adapters
  - 返回 canonical asset ID、exact analysis binding 和 projection freshness，不再给已分析图片返回 null revision。
- `core/photo_atlas.py`
  - 只消费 current-head-admitted canonical-ID projection；拒绝 orphan/stale/wrong-database/bad-digest rows。
- Electron/React/MCP/Codex consumers
  - 采用同一 public image evidence contract；legacy alias 只用于输入兼容。

## Delivery phases

### Phase 0 — Freeze the red baseline

- 用 controlled fixture 固定当前 canonical-only import、legacy-only ingest、canonical-first/legacy-second 和反向顺序的 rows/IDs。
- 将“image job 被拒绝、image import 不建 job、Atlas 读取 unmanaged `image_index`、mixed revision 为 null”写入失败测试。
- 记录 exact checkout 与审计命令；baseline RED 不是 waiver。

### Phase 1 — Contract and additive persistence

- 冻结 closed Image Analysis Result、job request、projection receipt/manifest 与 alias contracts。
- 添加 next-version migration、checksum、preflight/backup/recovery 与 schema verification。
- 添加 immutable result、alias no-rebind、receipt no-update/delete、head referential integrity 和 verified-read tests。
- 对已有数据库做只读 preflight；未知 legacy schema 或 ambiguous alias/duplicate 必须停止迁移并报告。

### Phase 2 — Durable image job and source admission

- 扩展 `create_analysis_job()` 和 import plan 支持 image；注册唯一 `image_analysis` worker kind。
- 把 EXIF、geocode、vision、embedding、quality 拆为 closed stages 和 durable checkpoints。
- Enqueue 固定 exact database/root/source/asset/profile/expected-head binding；每次 attempt 和 publish 前重新验证。
- 实现 cancel、heartbeat、stale lease recovery、bounded retry 和 terminal outcome。
- Ephemeral upload 若要持久发布，必须先进入批准 managed source；否则只能是明确的 non-persistent diagnostic。

### Phase 3 — Immutable publication

- 把 stage artifacts 规范化为 Image Analysis Result v1，验证 closed schema 与 deterministic digest。
- 在一个 `BEGIN IMMEDIATE` 中完成 result append、head CAS、change append、publish receipt 和 job checkpoint。
- 相同 job/retry 返回同一 publication；idempotency key rebind、stale head、source changed、DB changed 全部在写前拒绝。
- current image read 只采用验证过的 head/result binding；tampered or partial ledger fail closed。

### Phase 4 — Canonical-ID shadow and alias bridge

- 实现 deterministic legacy row renderer 和 idempotent projector。
- 同事务应用 canonical-ID `image_index` row、alias delta 与 projection receipt。
- 建立单调 change cursor；乱序旧 change 只产生 superseded receipt。
- Backfill 先按 SHA/source 解析 existing canonical asset，再建立不可重绑定 alias；不根据 `img_*`/`asset_*` 前缀猜测身份。
- 实现 fixed-high-water-mark rebuild、path-free parity manifest 和完整 generation activation。

### Phase 5 — Consumer cutover

- Search、Inbox、Memories、Create、Codex 先进入 shadow comparison，再切到 canonical image contract。
- Atlas rebuild/query 只接受 matching current-head receipt，输出 canonical ID/revision；旧 unmanaged row 变成 parity gap。
- 在同一 fixture matrix 中比较各 surface 的 asset ID、analysis binding、availability 和 error semantics。
- manifest 全绿后禁用 legacy direct write；保留 alias read adapter 和上一完整 projection generation 作为可恢复读路径。

### Phase 6 — Fault, restart, order and parity gate

- 在 enqueue、每个 analysis checkpoint、publish transaction、projector transaction、manifest build 和 active switch 注入 crash。
- 覆盖同路径 source replacement、同路径 DB replacement、active Library switch、root revoke、stale runtime 和 wrong profile。
- 运行 100 轮 idempotency replays、CAS races 和 out-of-order changes。
- 删除全部 A0.1 projection，固定 high-water mark 重建，验证 manifest digest 和 canonical ledger immutability。
- 在完整 final diff 上运行 focused tests、production-surface oracle、offline gate 和整仓 regression；然后才写 implementation evidence/status。

## Failure recovery rules

1. **分析未发布**：只有 job/checkpoint；允许从安全 stage 重试，不能创建 head。
2. **publish transaction 失败**：result/head/change/receipt/job checkpoint 全回滚。
3. **publish 已成功、projector 未运行**：canonical head 有效，projection 标记 pending；从 durable change cursor 恢复。
4. **projector transaction 失败**：row/alias/receipt 全部保持旧状态；相同 change 安全重放。
5. **manifest 未激活**：消费者继续使用上一完整 generation。
6. **stale/out-of-order change**：记录 superseded，不修改 row/head。
7. **same-path source/DB replacement**：稳定 scope error，零领域写入；用户重新 enqueue 新 operation。
8. **legacy alias conflict**：停止该 asset/backfill batch并输出 path-free conflict；不得猜测或 rebind。

## Rollout flags and ownership

实施可使用三个互相独立、默认 fail-closed 的 rollout controls：

- canonical image enqueue/publication；
- shadow projector + parity evidence；
- canonical consumer read generation。

这些 control 只决定启用次序，不创建双真源。legacy direct writer 必须有单一 owner、可审计调用点和明确 retirement gate；read rollback 不允许恢复 unrestricted legacy write。

## Verification commands to provide during implementation

实施时必须交付一个单一 A0.1 verify entrypoint，至少串联：

- contract/migration/immutability tests；
- durable job restart/cancel/admission tests；
- publish CAS/idempotency/fault tests；
- projector/alias/parity/rebuild tests；
- Search/Inbox/Memories/Create/Codex/Atlas identity matrix；
- production inventory exact oracle 与 scoped offline regression。

精确 pass count、commands、digests 和 residuals 记录在同目录 `implementation-evidence.md`；该记录只允许来自 current-disk exact checkout，不用计划值或预测值替代。

## Promotion gate

在以下条件同时满足前保持 `NOT IMPLEMENTED` 或 `IMPLEMENTED / VALIDATION PENDING`：

- production image import 确实创建 durable image job；
- canonical publish 不经过 legacy-first write；
- immutable result/head/change/receipt 原子性与恢复通过；
- canonical-ID projection、alias 和 manifest 可重建；
- Atlas 及所有 current consumers 采用同一 canonical ID/revision；
- restart、乱序、same-path DB/source replacement 与 idempotency matrix 全绿；
- parity diff 清零并禁用 legacy direct write。

即使上述 controlled-local 条件全部通过，T001–T003 historical RED baseline、真实 native/provider/Codex/DeepSeek host、大型用户库、clean machine、remote CI 与 release 未验收时，仍只能标记 `CONTROLLED-LOCAL GATES PASSED / PROMOTION BLOCKED`，不能标记 `LOCALLY VALIDATED`。
