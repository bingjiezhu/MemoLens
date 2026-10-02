# Feature Specification: Canonical Image Publication + Shadow Projection

- Feature ID：`ML-008-A0.1`
- 创建日期：2026-08-29
- 实施状态：`IMPLEMENTED`
- 验证状态：`CONTROLLED-LOCAL GATES PASSED / PROMOTION BLOCKED`
- 父规范：[ML-008 Unified Media Memory Kernel](../../spec.md)
- 优先级：P0 架构收敛
- 范围：canonical image publication、durable `image_analysis` job、legacy shadow projection 与 consumer cutover
- 后续：ML-008-B versioned embedding/projection；本切片不声称解决 Atlas 的向量语义或复杂度问题

## Objective

把图片从“先写 mutable `image_index`，再尽力同步 media asset”的双真源链，收敛为单向发布链：

```text
approved Library source
  → canonical Asset + Source Observation
  → durable image_analysis job
  → immutable Image Analysis Revision
  → exact Analysis Head CAS
  → durable projection change
  → canonical-ID legacy shadow projection
  → parity receipt / manifest
  → Search / Inbox / Memories / Create / Codex / Atlas consumers
```

唯一业务真相是 canonical `Asset`、`Source Observation`、不可变 `Image Analysis Revision` 和显式 `Analysis Head`。`image_index` 及 Atlas 索引只允许是从 exact canonical head 重建的 projection，不得继续拥有独立图片事实。

## Current Truth: production path implemented, promotion blocked

当前 shared worktree 已实现 A0.1 的主纵向链，不再是本切片创建时的 legacy-first 状态：

- V14 引入 immutable Image Analysis Result/head、job/attempt/checkpoint、publish/change/projection receipt、generation/manifest/row 和 legacy alias authority；后续 additive migrations 保留该权威，当前 production schema 是 V17，并有 exact schema manifest、immutability/no-rebind triggers 与 downgrade guard。
- persistent image import 会先解析 managed Asset/Source，创建 durable `image_analysis` job；legacy HTTP surface 只是返回 `202 + stable job ID` 的 compatibility adapter，不再在请求线程内完成 full analysis。
- production runner 按 source admission、metadata、geocode、vision、embedding、quality、publish 和 shadow projection 运行 closed stages；checkpoint 只保存 outcome/artifact digest 与 bounded reason，cancel/restart 不会把 partial stage 提升为 head。
- source read 从 approved root + relative identity 开始，使用 no-follow handle 并在 publish 前重验 database/runtime/source binding；raw absolute path 不构成发布权限。
- canonical publish 在一个 `BEGIN IMMEDIATE` 中完成 result append、exact head CAS、projection change、publish receipt 与 job checkpoint；projector 以 canonical asset ID 物化 `image_index` shadow row 并写 projection receipt/manifest/alias snapshot。
- legacy-only backfill 是显式、幂等、database/root/source-scoped 的路由；不根据 ID 前缀猜身份，alias 不可 rebind。
- mixed current image 会返回 exact analysis binding；Atlas 只消费 matching current-head receipt 的 canonical-ID row；Codex plugin `wiki_open` 只在 exact-current result/source/receipt、V17 sealed clean read manifest、generation rows/aliases 与完整 physical census 全部一致时输出 typed current observation。
- V16 增加 one-shot provider egress grant/manifest authority；provider vision 只可从 native 已封存 intent + process-private token 开始，在 derivative decode 和 send 前重验 exact DB/job/request/source/profile/runtime/attempt。发送开始即消耗 grant，known failure、unknown/ambiguous outcome 都不自动重试；持久层不保存 token、permit、path、原图、wire body 或 raw provider response。
- V17 增加 sealed canonical image read manifest；current read 只在 canonical/read manifest fields、raw JSON 和 SHA exact equality，predecessor 合法，generation rows/aliases 与全 `image_index` physical census 无差异时激活。dirty V16 migration 无可信 predecessor 时显式 unavailable，不伪造 rollback lineage。

但这仍不是 `LOCALLY VALIDATED`，下列 correction/promotion blocker 必须保留：

- provider vision 的 exact grant + byte-bound payload manifest + one-shot send + canonical publish 已在 controlled-local mock transport 实现；生产默认不配置 grant resolver，因此仍会在 decode/DB/send 前显式 `provider_not_authorized`。真实 native issuer、真实凭据/provider 和 clean-machine host 验收未完成；geocode remote stage 仍为 disabled，不属于本次 T034 image-vision 实现证据。
- publish/projector 的 logical before/after fault matrix、四类各 100 轮 replay/CAS/order matrix、source/SQLite 同路径 replacement 冷重启，以及 enqueue 与每个 analysis checkpoint 的 18 格外部 kill/restart matrix 已在 controlled-local 通过；文档冻结后的 A0 verify 与全量 `npm run check` 也在同一实施集上通过，且 gate 前后 full-tree snapshot 一致。
- same-high-water rebuild、source-removal/derived physical retirement、全 consumer parity-zero read cutover、legacy direct-write 关闭与 identity/revision matrix 已在 controlled-local 实现。T075B 如要删除 V15 引入的 immutable audit evidence，必须另立 schema/retention contract；它不是 A0.1 当前 blocker。
- T001–T003 historical red-baseline artifact 尚未补录；真实 native grant issuer/product wiring、credential/provider host、live Codex 与 DeepSeek Harness host、大型用户库、clean-machine、remote CI 和 release 也未验收。

因此当前状态是 `IMPLEMENTED / CONTROLLED-LOCAL GATES PASSED / PROMOTION BLOCKED`。本文件的 success criteria 与 promotion rule 仍是最终边界，controlled-local green 不会自动勾销未覆盖的实机与发布边界。

## Authority and truth boundaries

1. 文件系统或系统媒体库提供原始 bytes；路径不是资产身份，也不是写权限。
2. `assets.sha256` 和受控 `asset_sources` 确定内容与来源；byte-identical 内容只产生一个 canonical asset。
3. `image_analysis` job 只负责执行和恢复，不拥有最终分析真相。
4. 发布后的 Image Analysis Revision 不可变；current 只由 Analysis Head 选择。
5. canonical publish 与 projection-change record 必须同事务提交；projection 可以稍后重试，但不能反向改写 canonical revision。
6. `image_index`、Atlas、Search adapter 和其他 current views 都是可删除、可重建的 derived state。
7. legacy image ID 只是受 database/library scope 约束的 alias，不得成为新的 canonical ID，也不得用来绕过 source 或 database admission。

## User Stories

### US1：一次图片导入形成可恢复的 canonical 分析（P0）

用户导入图片后立即得到稳定 operation/job ID。任务可以在 metadata、geocode、vision、embedding、quality、publish 或 projection 阶段被取消、杀进程并恢复；未完成结果不成为 current。

**Independent test**：对每个 checkpoint 前后杀进程并重启，最终只得到一个 job、一个 intended analysis revision、一个 head 和一个 exact projection receipt。

**Acceptance scenarios**：

1. **Given** 一个已批准 Library 内的 image source，**When** 创建分析任务，**Then** API 返回 `202` 和稳定 `image_analysis` job ID，不在请求线程内完成模型分析。
2. **Given** worker 在任一 stage 后崩溃，**When** 同一 database runtime 重启，**Then** job 从安全 checkpoint 恢复或进入明确终态，不重复发布 revision。
3. **Given** 用户请求取消，**When** worker 到达下一个安全 checkpoint，**Then** job 进入 cancelled，partial artifact 不成为 head 或 projection。
4. **Given** 同一 idempotency key 和相同请求重试，**When** enqueue 再次执行，**Then** 返回同一 job；相同 key 绑定不同 source/profile/database 时返回 conflict。

### US2：只有 exact source 和 exact database 可以发布 head（P0）

Job 必须固定 database identity、runtime generation、Library/root capability、source identity、asset SHA 和 analysis profile。任一身份变化都必须在 canonical write 前失败。

**Independent test**：在 hash 后替换同路径文件、替换同路径 SQLite、切换 active Library、撤销 root 或用旧 runtime 提交，断言 revision/head/change record/projection 均没有部分写入。

**Acceptance scenarios**：

1. **Given** source 在 admission 后被同路径不同 bytes 替换，**When** worker 准备发布，**Then** 返回 `source_changed`，不发布 revision/head。
2. **Given** SQLite 路径不变但其 database UUID、文件身份或 runtime generation 已改变，**When** 旧 job 恢复，**Then** 返回 `database_scope_changed`，不向新旧任一 active ledger 写半个结果。
3. **Given** 两个任务都基于同一 expected head，**When** 一个任务先发布，**Then** 另一个 CAS 失败或显式 rebase；不得 silent overwrite。
4. **Given** revision 2 已成为 current，**When** 延迟到达的 revision 1 projection change 被处理，**Then** 它记录 `superseded` receipt，不能回退 head 或 shadow row。

### US3：legacy surface 只看到 canonical-ID shadow（P0）

现有 Photo Search/Create 等 surface 可以在迁移期继续消费兼容字段，但 row identity 必须是 canonical asset ID，字段必须由 exact current image revision 确定性生成。

**Independent test**：先 canonical import 再 legacy ingest、反向执行、并发执行与重复执行同一 bytes，最终 canonical asset、shadow row、所有 current result 只暴露同一个 asset ID；旧 ID 仅能作为不可重绑定 alias 解析。

**Acceptance scenarios**：

1. **Given** canonical asset 已存在，**When** legacy ingest 发现同一 SHA，**Then** 不创建第二个 `img_*` identity；若已有 legacy ID，则新增 alias 到现有 canonical asset。
2. **Given** 旧数据库只有 `img_*` row，**When** backfill 运行，**Then** 先解析或创建唯一 canonical asset，再以 canonical ID 生成 shadow row，并保存不可重绑定 alias。
3. **Given** legacy API 输入一个有效 alias，**When** 读取资源，**Then** alias 在 exact database/library scope 内解析并返回 canonical `asset_id` 与 analysis binding。
4. **Given** alias 已绑定 asset A，**When** 任何路径尝试把它绑定到 asset B，**Then** fail closed 为 `legacy_alias_conflict`。
5. **Given** shadow projector 重试相同 change，**When** input binding 未变，**Then** projected row digest 与 receipt 不变，不产生重复或第二身份。

### US4：Atlas 与所有 current consumers 只能消费已证明的 head（P0）

Atlas 不再把任意 mutable `image_index` row 当成业务事实。它与 Search、Inbox、Memories、Create、Codex 必须只采用具有 current-head binding 和有效 projection receipt 的 canonical-ID row。

**Independent test**：注入 orphan row、旧 head row、错 database receipt、缺失 manifest 和损坏 row digest；每个 consumer 均拒绝或明确显示 projection unavailable，不静默采用污染数据。

**Acceptance scenarios**：

1. **Given** `image_index` row 没有匹配 current head 的 receipt，**When** Atlas rebuild，**Then** 该 row 不进入 Atlas generation，并产生可诊断 parity gap。
2. **Given** projection 被删除，**When** 从 fixed canonical high-water mark 重建，**Then** canonical ledger 不变，重建 manifest 的确定性 hash 与基线一致。
3. **Given** projection build 在激活前崩溃，**When** consumer 继续读取，**Then** 仍使用上一个完整 generation，不读取半建集合。
4. **Given** current head 已发布但 shadow projection 暂时失败，**When** canonical reader 查询，**Then** 返回 current canonical revision 和显式 `projection_pending`；legacy consumer 不把旧 row 伪装成 current。

## Functional Requirements

### Canonical identity and job

- **FR-A0.1-001**：所有受支持的持久 image ingest surface 必须先建立或解析 canonical Asset/Source；不得从 `image_index.id`、文件名或 raw path 推导新的真源身份。
- **FR-A0.1-002**：byte-identical 内容必须解析到唯一 `assets.id`；多个路径保留多个 Source Observation，同一路径内容替换必须建立新 asset 并保留旧历史。
- **FR-A0.1-003**：持久图片分析必须使用 closed worker kind `image_analysis`；unknown kind 或把 image 路由到 `video_index` 必须失败。
- **FR-A0.1-004**：enqueue 必须返回 `202`、稳定 job/operation ID 和幂等 receipt；交互 HTTP 请求不得同步执行完整分析。
- **FR-A0.1-005**：job 至少持久化 database UUID、runtime generation、root/source/asset/profile binding、attempt、stage、checkpoint、heartbeat、cancel request、error outcome 和 intended analysis run/revision。
- **FR-A0.1-006**：stage registry 必须 closed，并至少区分 source admission、metadata、geocode、vision、embedding、quality、canonical publish 和 shadow projection；unsupported/disabled 能力必须成为显式 stage outcome。
- **FR-A0.1-007**：checkpoint 只保存恢复所需的 bounded metadata/artifact reference，不保存 credential、绝对路径、未受控原始图片或可逆 provider payload。

### Database and source admission

- **FR-A0.1-008**：job 创建、每次 attempt、provider/file read 前和 canonical publish 前都必须验证 exact active database binding；同路径 DB replacement、database UUID 变化、文件身份变化或 runtime generation 变化都必须先于写入被拒绝。
- **FR-A0.1-009**：source read 必须从批准 root capability 和相对 source identity 开始，使用 no-follow/FD identity 规则；客户端提供的 absolute path 不构成 authority。
- **FR-A0.1-010**：job 必须固定 input asset SHA、source identity、observed size/mtime/file identity；publish 前重新验证 bytes identity，变化时不得复用旧分析结果。
- **FR-A0.1-011**：A0.1 不签发新的网络权限。任何 remote vision/geocode/embedding step 必须继续受当前 network profile、provider opt-in/grant 和 payload manifest 约束；无权限时显式降级或失败。

### Immutable publication and head CAS

- **FR-A0.1-012**：Image Analysis Revision 必须是 versioned、closed、不可变结果，固定 asset SHA、analysis profile、producer/model/rule provenance、每个 stage outcome 和 canonical content digest。
- **FR-A0.1-013**：lifecycle run 可以在发布前推进状态，但一旦 revision 成功发布，其 result document、input binding、revision number 和 digest 不得 update/delete；修正只能追加新 revision。
- **FR-A0.1-014**：Analysis Head 必须绑定 `{asset_id, analysis_run_id, revision, content_sha256}`，并使用 expected prior head 做 CAS；partial、failed、cancelled、source-mismatched 或 tampered revision 不得成为 current。
- **FR-A0.1-015**：revision/result append、head CAS、projection change/outbox append、publish receipt 和 job publish checkpoint 必须在同一个 `BEGIN IMMEDIATE` 事务中提交或全部回滚。
- **FR-A0.1-016**：同一 enqueue idempotency key 只可绑定一个 canonical request digest；同一 job 的 worker 重试只能返回同一 intended publication，不能追加重复 current revision。
- **FR-A0.1-017**：current image read contract 必须返回 canonical `asset_id`、exact analysis binding、source availability 和 projection freshness；不得继续以 nullable analysis binding 冒充已分析素材。

### Canonical-ID shadow projection and aliases

- **FR-A0.1-018**：canonical publish 是唯一上游写入。`indexing/pipeline.py` 的新持久路径不得先写 `image_index`；`ImageIndexRepository.upsert` 在切换后只能由受控 projector/迁移 adapter 调用。
- **FR-A0.1-019**：shadow row 的主身份必须等于 `assets.id`，其字段必须由 exact current Image Analysis Revision 和 admitted current source 确定性渲染。
- **FR-A0.1-020**：legacy ID mapping 必须存入 append-only、不可重绑定、database-scoped alias ledger。典型 `img_*` 前缀只是旧命名约定，不能用前缀判断某 ID 是否 canonical。
- **FR-A0.1-021**：alias resolution 只能作为输入兼容层；所有 current contract、projection receipt、Atlas row、Codex evidence 和新创作引用必须输出 canonical asset ID。
- **FR-A0.1-022**：projector 必须按 monotonic change position 消费，并在 apply 时重验 exact current head。旧 revision、错 database、错 source 或损坏 digest 只能产生 blocked/superseded outcome，不能修改 active row。
- **FR-A0.1-023**：projection row、alias changes 和 per-change receipt 必须同事务写入；重试相同 change 必须得到同一 row digest 和 receipt。
- **FR-A0.1-024**：无 available admitted source 的 asset 不得继续作为 current legacy result；projector 必须生成显式 removal/unavailable outcome，而不删除 canonical history。

### Parity evidence and consumers

- **FR-A0.1-025**：每个 applied/no-change/superseded/blocked change 必须生成 Image Projection Receipt，至少固定 projection contract/version、database UUID、change position、canonical asset ID、analysis binding、source binding digest、projected row digest、alias-set digest 和 outcome。
- **FR-A0.1-026**：每次 backfill/rebuild/cutover 必须生成 path-free Image Projection Manifest，固定 canonical high-water mark、compiler version、sorted asset/head/expected/actual digests、alias digest、counts 和 missing/unexpected/mismatched 分类。
- **FR-A0.1-027**：receipt/manifest 的领域 digest 必须排除 wall-clock timestamp、generation nonce 和展示布局；时间只存在于 envelope。相同 canonical snapshot 重建必须得到相同 manifest digest。
- **FR-A0.1-028**：只有 manifest `missing=unexpected=mismatched=0` 且所有 row receipt 匹配 current head 时，read cutover 才可激活；失败时继续使用上一完整 generation或显式 unavailable，不能读取半建 projection。
- **FR-A0.1-029**：Search、Inbox、Memories、Create、Codex 和 Atlas 必须采用同一 canonical asset ID 与 analysis binding；consumer 不得直接信任无 receipt 的 `image_index` row。
- **FR-A0.1-030**：Atlas A0.1 adapter 必须拒绝 orphan、stale-head、wrong-database 和 digest-mismatched row，并输出 canonical identity。A0.1 不得据此宣称 Atlas 的 O(N²)、混向量空间或视觉语义问题已解决。

## Required Receipt and Manifest Semantics

### Image Projection Receipt

Receipt 的 canonical content 至少包含：

- `projection_contract = legacy-image-index-shadow/v1`
- `database_uuid` 与 `change_position`
- `asset_id`
- `analysis_binding = {analysis_run_id, revision, content_sha256}`
- `source_binding_sha256`
- `projected_row_sha256`（blocked/removal outcome 可为空）
- `alias_set_sha256`
- `outcome = applied | no_change | superseded | removed | blocked`
- `reason_code` 和 `receipt_sha256`

它不得包含绝对路径、文件名、用户文本、图片 bytes 或 provider payload。

### Image Projection Manifest

Manifest 针对一个 fixed canonical high-water mark，至少包含：

- schema/projector/compiler version；
- database UUID 与 canonical ledger position；
- eligible canonical image count、projected count、alias count；
- 按 canonical asset ID 排序的 head binding 与 expected/actual row digest；
- missing、unexpected、mismatched、blocked 的稳定 reason counts；
- alias-set digest、row-set digest 和 manifest digest。

生产日志可以只保存 summary；完整 path-free manifest 作为 promotion evidence 保留。任何 diff 都必须分类，不能用 tolerance 掩盖 identity/head mismatch。

## Failure and Recovery Matrix

| 注入点 | 允许状态 | 禁止结果 |
| --- | --- | --- |
| job row 提交前/后 | 无 job，或一个可恢复 queued job | 重复 job / 无 receipt 的 in-flight work |
| 任一分析 checkpoint | running/interrupted/failed，可重试 | partial result 成为 head |
| canonical publish 事务中 | 全回滚，或 revision+head+change+receipt 全部存在 | revision 无 head、head 无 change、job 假 succeeded |
| canonical publish 后、projection 前 | current canonical head + `projection_pending` | 反向撤销 canonical revision |
| projection row/alias/receipt 事务中 | 旧完整 row 或新完整 row | row、alias、receipt 半写 |
| manifest build/active switch | 上一完整 generation 继续可读 | consumer 读取半建 generation |
| revision 乱序到达 | stale change 记为 superseded | current row 回退 |
| 同路径 source replacement | `source_changed`，零 publish write | 新 bytes 使用旧 analysis |
| 同路径 DB replacement | `database_scope_changed`，零 domain write | 旧 job 写入新 active DB |

## Success Criteria

- **SC-A0.1-001**：canonical image import 在 clean database 中产生 `1 asset + admitted sources + 1 durable job + 1 immutable revision + 1 head + 1 projection receipt`；分析成功时 current result 的 analysis binding 非空率为 100%。
- **SC-A0.1-002**：canonical-first、legacy-first、并发和重复 ingest 的 byte-identical fixtures 中 canonical asset 数恒为 1，consumer-visible asset ID 差异数为 0。
- **SC-A0.1-003**：对每个 job/publish/projector checkpoint 做 kill/restart，重复 head、重复 revision、orphan change 和半写 projection 数均为 0。
- **SC-A0.1-004**：同路径 source 与 DB replacement fixtures 中，错误 ledger 写入数为 0，稳定错误分类准确率为 100%。
- **SC-A0.1-005**：100 轮 stale-head CAS 与 out-of-order projection 中 silent overwrite/rollback 次数为 0。
- **SC-A0.1-006**：同 idempotency key 同 request 重试 100 次只返回一个 job/publication；key rebind 全部 conflict。
- **SC-A0.1-007**：删除并从同一 high-water mark 重建 shadow projection 后 manifest digest 完全一致；canonical row/count/digest 变化为 0。
- **SC-A0.1-008**：Search、Inbox、Memories、Create、Codex、Atlas fixture matrix 对同一图片返回相同 canonical asset ID 和 exact analysis revision，orphan/stale projection 采用次数为 0。
- **SC-A0.1-009**：read cutover 前 parity manifest 的 missing、unexpected、mismatched、blocked 均为 0；没有 waiver 或“已知可忽略”身份差异。

## Rollout and Rollback

1. 冻结 current legacy/canonical split 的可重放 red baseline。
2. 只启用 canonical image job/publication，projection 在 shadow 模式构建并生成 receipts/manifests；消费者仍使用上一完整读 generation。
3. 运行历史 backfill 和双入口对照；任何 unexplained diff 保持 gate RED。
4. parity 清零后，先让 current consumers 读取 canonical-ID projection，再禁用 legacy direct write。
5. 回滚只允许把 read pointer 切回上一完整 projection generation或暂停新 job；不得删除 canonical revisions、重新启用长期双真源，或把 legacy row 反向覆盖 canonical head。

## Explicit Non-goals

- 不在本切片选择新的 image embedding 模型、ANN、聚类或 Atlas 布局算法。
- 不宣称修复 `semantic_hash` 的视觉语义、不同向量空间混算或 Atlas O(N²)；这些属于 ML-008-B。
- 不进行一次性全库换表、删除历史 `image_index` 或大规模搬动原始媒体。
- 不新增 standalone audio ingest、人物识别或未授权远端分析。
- 不让 Atlas、Codex、plugin 或 UI 直接写 canonical image revision。
- 不把 focused tests、fixture parity 或本地重启 harness 外推为真实用户库、远端 provider 或 release validation。

## Promotion Rule

- 仅创建 schema/contract 或复用现有 generic tables 时，状态仍是 `NOT IMPLEMENTED`。
- 完成生产 enqueue、worker、publish、projector、alias 与 consumer wiring，但尚未跑完整 failure/parity matrix 时，最多标记 `IMPLEMENTED / VALIDATION PENDING`。
- 只有本规范全部 success criteria 在同一 exact checkout 通过、manifest diff 清零且 legacy direct write 已关闭，才能标记 `LOCALLY VALIDATED`。
- clean-machine、真实大型用户库、真实 provider 和发布验收必须另行记录，不能由 A0.1 本地证据替代。
