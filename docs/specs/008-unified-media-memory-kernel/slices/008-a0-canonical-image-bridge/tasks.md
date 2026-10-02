# Tasks: ML-008-A0.1 Canonical Image Publication + Shadow Projection

> 当前实施状态：`IMPLEMENTED / CONTROLLED-LOCAL GATES PASSED / PROMOTION BLOCKED`。`[x]` 只表示 production code 与对应 controlled-local 证据已进入同一实施集并通过冻结后 gate；未勾选项仍是 promotion blocker，不能由 focused green 外推完成。

## Phase 0 — Audited red baseline

- [ ] T001 冻结 canonical-only、legacy-only、canonical-first/legacy-second、legacy-first/canonical-second 四组 image fixtures 的 rows、IDs、head/job/projection 差异。
- [ ] T002 增加 current-gap tests：image analysis job 被拒绝、image import 不建 job、mixed image revision 为 null、Atlas 接受 unmanaged `image_index`、缺少 alias ledger。
- [ ] T003 记录 exact checkout、database schema fingerprint 与 path-free baseline artifact；明确 RED 不是 waiver 或 implementation evidence。

## Phase 1 — Closed contract and additive schema

- [x] T010 冻结 Image Analysis Result v1 closed schema、limits、canonical encoding、content digest 和 typed stage outcomes。
- [x] T011 冻结 `image_analysis` request/job/checkpoint/error/idempotency contract 与 closed stage registry。
- [x] T012 冻结 Image Projection Change、Publish Receipt、Projection Receipt、Projection Manifest/Generation contracts。
- [x] T013 冻结 database-scoped Legacy Image Alias contract，要求 append-only、no-rebind、canonical output only。
- [x] T014 添加 next-version additive migration、checksum、backup/preflight/recovery、physical schema verification 与 downgrade guard。
- [x] T015 添加 published result/receipt/alias immutability triggers、head foreign keys 和 strict verified-read tests。
- [x] T016 对 unknown legacy schema、ambiguous SHA/source、duplicate canonical identity 和 alias conflict 做 fail-closed migration tests。

## Phase 2 — Durable `image_analysis` job

- [x] T020 扩展 canonical analysis job creation 支持 image，固定 database/runtime/root/source/asset/profile/expected-head binding。
- [x] T021 修改 media import plan，使 persistent image import 在同一受控流程中建立/解析 Asset/Source 并 enqueue image job。
- [x] T022 注册唯一 `image_analysis` worker kind；unknown、duplicate 和 image→video worker substitution 明确拒绝。
- [x] T023 将 metadata、geocode、vision、embedding、quality、publish、shadow projection 拆为 closed durable stages/checkpoints。
- [x] T024 实现 heartbeat、attempt、cancel、stale lease recovery、bounded retry 和 interrupted/partial/terminal 状态机。
- [x] T025 把 legacy indexing production route 改为 `202 + stable job ID` compatibility adapter，移除 HTTP 内同步 full analysis。
- [x] T026 要求持久 uploaded image 先进入批准 managed source；禁止 ephemeral/raw payload 旁路 source admission 发布 canonical result。

## Phase 3 — Exact database/source admission

- [x] T030 在 enqueue、attempt、每次 file/provider read 和 publish 前验证 database UUID、DB file identity 与 runtime generation。
- [x] T031 通过 approved root capability + relative source identity + no-follow FD 读取图片；raw absolute path 不构成 authority。
- [x] T032 固定并重验 asset SHA、source ID、size/mtime/file identity；同路径 source replacement 返回 `source_changed` 且零 publish write。
- [x] T033 同路径 SQLite replacement、active Library switch、old runtime 和 revoked root 返回稳定 scope error 且零 domain write。
- [x] T034 remote stage 复用现有 network/provider grant 与 payload manifest；offline/未授权时在 send 前显式 deny 或降级。

## Phase 4 — Immutable publication and CAS

- [x] T040 将真实 stage outputs 规范化为 immutable Image Analysis Result，保留 producer/model/rule 与 signal provenance，不夸大 text-derived embedding。
- [x] T041 实现一个 `BEGIN IMMEDIATE` publish：result append + exact head CAS + projection change + publish receipt + job checkpoint 全部提交或回滚。
- [x] T042 同一 job/attempt retry 只返回同一 intended revision/head/change；idempotency key rebind 必须 conflict。
- [x] T043 拒绝 partial/failed/cancelled/source-mismatched/tampered result 成为 head。
- [x] T044 current image read 返回 canonical asset ID、exact run/revision/content digest、source availability 和 projection freshness。
- [x] T045 修改 mixed image candidate，使已发布图片的 analysis run/revision 非 null；未分析图片明确为 pending/unknown，不伪装 current。

## Phase 5 — Canonical-ID shadow and legacy aliases

- [x] T050 实现 exact Image Analysis Revision → legacy-compatible row 的 deterministic renderer；row ID 必须等于 canonical asset ID。
- [x] T051 实现 monotonic、idempotent shadow projector，并在 apply 时重验 database、current head、source 与 digests。
- [x] T052 同事务写 projection row、alias delta 与 receipt；任一点 fault 不得留下半写。
- [x] T053 限制 `ImageIndexRepository` 的 production mutation owner；canonical job 不再 legacy-first/direct-upsert。
- [x] T054 Backfill legacy-only rows：按 SHA/source 解析唯一 canonical asset，保留 `img_*` 为不可重绑定 alias，不按前缀猜身份。
- [x] T055 覆盖 canonical-first 后 legacy ingest、legacy-first 后 canonical import、并发与重复执行，证明 canonical asset/consumer ID 恒为一个。
- [x] T056 实现 source unavailable/removal projection outcome：Tx A 以 exact current head/source/DB CAS 原子标记 source terminal 并 append rowless remove change；Tx B 独立生成 verified `removed` receipt 与 successor generation，不删除 canonical revision/history。
- [x] T057 乱序旧 revision change 只生成 superseded receipt，不回退 active row 或 head。

## Phase 6 — Parity manifest and consumer cutover

- [x] T060 实现 path-free per-change Projection Receipt，固定 exact analysis/source/row/alias bindings 与 deterministic digest。
- [x] T061 实现 fixed-high-water-mark Projection Manifest，输出 sorted row/alias digests 和 missing/unexpected/mismatched/blocked 分类。
- [x] T062 实现 projection delete/rebuild、新 generation 构建与 atomic active switch；事务失败保留调用前完整 active/physical state，rollback 只允许 immediate clean predecessor。dirty V16 迁移无可信 predecessor 时显式 unavailable，清理后才可建立新 clean root generation。
  - [x] T062A 实现 fixed-high-water closed rebuild API：仅从 canonical latest change/result/head/source/artifact 重新 render，runtime generation + nonce 共同绑定 same-H successor identity，同一 IMMEDIATE transaction 内重物化 A0-managed `image_index` 并 atomic switch active generation；任一 fault 回滚到调用前 active + physical 状态。
  - [x] T062B 完成 source removal outcome 与 V15 授权范围内的 derived-state retirement：latest remove 从后续 projector/rebuild snapshot 排除该 asset，原子 successor 清理 canonical + registered-alias physical rows，保留 alias ledger 作为 input adapter、保留 immutable generation/receipt evidence，且不影响 unmanaged/unrelated rows。
- [x] T063 Search、Inbox、Memories、Create 和 Codex 采用 canonical image evidence contract；legacy alias 只作为 input adapter。
- [x] T064 Atlas rebuild/query 只消费 matching current-head receipt 的 canonical-ID row，拒绝 orphan/stale/wrong-database/bad-digest row。
- [x] T065 明确保留 Atlas O(N²)、混向量空间与视觉语义为 ML-008-B residual；A0.1 状态不得掩盖这些缺口。
- [x] T066 V17 sealed clean read manifest 与 canonical manifest exact equality、generation rows/aliases 和完整 `image_index` physical census 全绿后才允许 current read；legacy direct write 已关闭，仅保留 Library-scoped alias input adapter 与 immediate clean predecessor rollback。

## Phase 7 — Fault, restart, order and idempotency validation

- [x] T070 在 enqueue 与每个 analysis checkpoint 前后 kill/restart，证明一个 job、一个 intended revision、零 partial head。
- [x] T071 在 publish transaction 每个边界注入 fault，证明 result/head/change/receipt/job checkpoint 原子提交。
- [x] T072 在 projection row/alias/receipt、manifest build 和 active pointer switch 每个边界注入 fault，证明无半建 consumer state。
- [x] T073 运行至少 100 轮 same-request replay、idempotency rebind、stale-head CAS 和 out-of-order change tests。
- [x] T074 运行同路径 source replacement 与同路径 DB replacement restart harness，验证稳定 error 和零错误 ledger write。
- [x] T075 删除全部 A0-managed physical shadow consumer projection 后从同一 high-water mark 重建，验证 manifest digest 相等且 canonical count/digest 不变。
  - [x] T075A 在当前 V17 下保留 V15 引入的 immutable generation/row/manifest/receipt/alias audit evidence；删除或破坏全部 A0-managed `image_index` consumer rows 后，可从同一 H 的 canonical 事实重建，old/successor manifest、rows、aliases semantic bytes 相等，canonical count/digest 不变，未登记 legacy-only row 与尚无 head/projection/alias 的 canonical Asset 兼容行保留。
  - T075B — `CONDITIONAL / N/A FOR A0.1`：删除 immutable audit generations/receipts 不是本切片要求；未来产品若要求，需另立 schema/retention contract，不绕过 V15 immutable triggers，也不作为当前 blocker。
- [x] T076 运行 Search/Inbox/Memories/Create/Codex/Atlas matrix，验证同一 canonical asset ID 与 exact analysis revision。

## Phase 8 — Gate and honest status

- [x] T080 提供单一 A0.1 verify entrypoint，串联 contract、migration、job、admission、publish、projector、alias、parity 与 consumer tests。
- [x] T081 在 final diff 上运行 production-surface inventory exact oracle、scoped offline gate、Python/Node regressions 与 diff/style checks。
- [x] T082 保存 exact checkout、commands、counts、manifest digest、fault matrix 和所有 residuals；不得用 focused green 外推真实 provider/release validation。
- T083 — `CONDITIONAL / NOT ENTERED`：只有 production wiring 完整但验证未结束时才使用 `IMPLEMENTED / VALIDATION PENDING`；它是过渡状态，不是 promotion 必经待办。
- [ ] T084 仅当全部 success criteria 在同一 exact checkout 通过、parity diff 清零、legacy direct write 关闭且 T001–T003 historical red baseline 完整时，才更新为 `LOCALLY VALIDATED`。当前仍受 real native/provider/Codex/DeepSeek host、大型用户库、clean machine、remote CI 与 release 未验收的 promotion 边界约束。
