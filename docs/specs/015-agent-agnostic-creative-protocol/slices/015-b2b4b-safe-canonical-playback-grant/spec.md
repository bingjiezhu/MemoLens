# Feature Specification: Safe Canonical Source Playback Grant

- Feature ID：`ML-015-B2B4B`
- 创建日期：2026-08-29
- 实施状态：`IMPLEMENTED / LOCALLY VALIDATED / PARENT B2B4 REAL-HOST JOURNEYS PENDING`
- 发布状态：`NOT COMMITTED / NOT RELEASED`
- 父切片：[ML-015-B2B4 Canonical Editor Handoff](../015-b2b4-codex-deepseek-canonical-editor-handoff/spec.md)
- 前置切片：[ML-015-B2B4A Verified Visual Replacement Review](../015-b2b4a-verified-visual-replacement-review/spec.md)
- 实施证据：[implementation-evidence.md](implementation-evidence.md)
- 优先级：P0 可见剪辑闭环

## Objective

让 Codex 和 DeepSeek Harness 共用的 canonical editor 能播放当前 Timeline 中已验、浏览器可解码的源视频，并让 ruler/playhead 与 clip 边界同步。播放是显式批准的短时 read capability，不是 Timeline 写入、Usage、render/export 或 final-fidelity 证据。

```text
native-approved timeline.preview_media capability
  → plugin asks Core for one short-lived project/head-bound lease
  → Core derives current clip → asset/source/SHA bindings
  → plugin keeps lease secret server-side
  → Browser asks only same-origin session + clip_id + Range
  → plugin proxy rechecks session/current snapshot
  → Core rechecks capability/head/source and streams one bounded range
  → Browser playhead renders source preview only
```

## Why the existing raw media route cannot be reused as authority

`GET /v1/assets/{asset_id}/media` 依赖 per-launch Desktop token，且它为 caller 指定的 asset 打开整个 source。下列方式均禁止：

- 不得把该路由放宽为普通 loopback read；这会让任意本地进程读取全库媒体。
- 不得把 Desktop token 交给 Browser、MCP result、URL、DOM 或 JavaScript。
- 不得让 plugin 代持一个超过当前 project/head/clip 的全库 Desktop token。
- 不得仅凭 Browser 提交的 `asset_id`、path、URL 或 source range 签发播放。

本切片使用独立 `timeline.preview_media` action 与独立 lease route，只复用已有 verified source opener 和 single-Range streamer 的数据面实现。

## Authority contract

### Explicit paired action

- `timeline.preview_media` 必须进入 next additive managed-schema capability union，由 native pairing presentation 明确显示。
- `timeline.apply_edit` 、`timeline.restore_revision` 不隐式授予 media read；preview-only 也不授予 edit/restore。
- 既有 capability 不因 migration 自动获得 preview action；用户需要对新 action 重新明确批准。
- preview lease 不消耗 canonical write operation 计数，但必须有独立、bounded 的租约/读取预算。

### Lease mint

Plugin 使用 server-held pairing credential 请求一个短时 lease。请求只包含 exact project/head 与随机 request nonce；Core 自行重读 Timeline/Coverage/source，导出允许的 current clip 集合。

冻结 transport：

- `POST /v1/agent/creative/projects/{project_id}/timeline/preview-leases`；body 只能是 `memolens.timeline_preview_lease_request@1`、`project_id`、`observed_head{revision,content_sha256}`、`request_nonce`，不能出现 clip/asset/source/path/URL。
- mint proof 使用独立 `MLPREVIEWMINT1` HMAC domain，绑定 capability、`POST`、canonical path、database UUID 与 exact body digest；不复用 write operation nonce。
- `GET|HEAD /v1/agent/preview-leases/{lease_id}/clips/{clip_id}/media` 使用独立 `MLPREVIEWREAD1` HMAC domain 与一次性 read nonce，绑定 lease、method、canonical path 和 normalized Range。
- server-to-server headers 冻结为 `X-MemoLens-Preview-Lease`、`X-MemoLens-Preview-Nonce`、`X-MemoLens-Preview-Proof`；Browser 不得持有或转发这些值。

Lease 至少固定：

- database UUID + runtime authority epoch；
- project ID + exact Timeline head/content digest + pinned Blueprint/Coverage bindings；
- capability ID + paired subject + expiry/revoke/max-read-budget state；
- 每个 current clip 的 clip ID、media kind、asset ID/SHA、source ID/binding digest、MIME、size 与 source-in/source-out presentation bounds；
- lease TTL、single-response bytes、aggregate bytes、request count 和 concurrency limits。

首版 exact budgets：TTL `90s`、single response `8 MiB`、aggregate `64 MiB`、requests `64`、concurrency `2`。合法 proof 在后续 scope/source 检查失败时仍消耗一次 nonce；已经为 GET 预留的 byte budget 不因断连退款。

Lease secret 只存在 Core 和 plugin server 内存。Core/plugin 任一进程重启、capability 过期/撤销、runtime epoch 变化、Timeline head 变化或 source identity 变化都使 lease 立即失效。

### Browser and proxy boundary

- Browser 只请求 `GET|HEAD /api/sessions/{session_id}/clip-media/{clip_id}`，可带一个标准 single `Range`。
- Browser 不得看到 backend URL、lease ID/secret、capability/proof secret、Desktop/Main token、asset/source identity 或绝对路径。
- editor server 先验 session cookie、Host/Origin、current snapshot 与 clip ID，再用 server-held lease 代理一次后端读取。
- Core 每次读取再验 capability definitive state、database/runtime/head/source/SHA/MIME 和 lease budget；不信任 plugin 自报“仍 current”。
- 只允许无 redirect、一个 Range、封闭 MIME allowlist 和有界 bytes；错 range 稳定返回 `416`。

## Playback semantics

- 首版是 **verified source preview**，只对 `probe_status=ready + video/mp4 + h264` 的已验 video source 开启。unsupported codec/container 保留 verified thumbnail 并明确显示不可播放；不伪装转码成功。
- lease 的 byte authority 是 clip 引用的 exact source asset；`source_in_ms/source_out_ms` 是 UI 播放窗口，首版不声称对容器 bytes 做时间片段隔离。需要严格 span-byte 限制时必须使用独立 derived transcode 后续切片。
- playhead 在 clip 内将 Timeline local time 映射到 `source_in_ms + local_ms`，到 `source_out_ms` 停止或进入下一 clip。
- image clip 仍使用 verified thumbnail 并按 canonical duration 推进时钟；不为图片开新 raw-byte route。
- source preview 默认静音。它不证明 subtitle、source audio、BGM、voiceover、mixing、transition、render 或 final fidelity。
- Play/Pause/Seek 只是视图状态，不写 Timeline、Usage 或任何 canonical ledger。Stage/Save/CAS/reread 语义保持不变。

## Functional requirements

- **FR-B2B4B-001**：新 capability action 必须由 native pairing 显式批准，不得从 edit/restore 隐式推导。
- **FR-B2B4B-002**：lease mint 只接受 project/head/nonce；clip/asset/source whitelist 由 Core 重读 current canonical resources 导出。
- **FR-B2B4B-003**：lease 必须 runtime/project/head/source-bound、short-lived、server-held、budgeted 并可由 capability revoke 立即终止。
- **FR-B2B4B-004**：Browser 只持有 editor session cookie 和 opaque clip ID；零 backend/lease/pairing/Desktop/Main secret 泄漏。
- **FR-B2B4B-005**：proxy 必须保持 exact `200/206/416`、`Accept-Ranges`、`Content-Range`、`Content-Length`、bounded MIME/ETag，禁止 redirect 与多 Range。
- **FR-B2B4B-006**：每次读重验 current Timeline head 与 source identity；stale/replaced/unavailable 在第一个 byte 前 fail closed。
- **FR-B2B4B-007**：UI 的 ruler/playhead/selected clip/Play/Pause/Seek 与 canonical timing 一致，pending trim/duration 只作 noncanonical preview。
- **FR-B2B4B-008**：unsupported codec、expired/revoked/stale lease、budget exhausted 有可诊断 fallback，不移动 canonical head。
- **FR-B2B4B-009**：production inventory 必须同时列出 lease mint、backend media read 和 editor proxy routes，并用 exact negative oracles 绑定权限/范围拒绝。
- **FR-B2B4B-010**：Codex 与 DeepSeek 使用同一 canonical editor/MCP/lease 实现；不建 host-local playback project。

## Stable errors

| Code | Meaning |
| --- | --- |
| `agent_preview_scope_denied` | capability 未包含 `timeline.preview_media` 或 project/head/clip 不属于该 scope |
| `agent_preview_lease_expired` | lease TTL、runtime epoch、capability state 或读取预算失效 |
| `agent_preview_head_changed` | exact canonical Timeline head 已变化 |
| `agent_preview_source_changed` | source ID/SHA/file identity 已变化或不可用 |
| `agent_preview_media_unsupported` | media kind/MIME/codec 不在首版 allowlist |
| `agent_preview_range_invalid` | Range 非法、多段或超出预算 |
| `agent_preview_proof_invalid` | mint/read proof 不属于 exact domain 或被替换 |
| `agent_preview_nonce_invalid` | mint/read nonce 缺失、过期或重放 |
| `agent_preview_rate_limited` | runtime lease store 已达上限 |
| `editor_session_unauthorized` | Browser session cookie 缺失或错误 |
| `editor_origin_invalid` | Browser 请求不是当前 same-origin editor |

## Required validation

1. next-schema migration：fresh/populated predecessor，历史 capability/receipt/event bytes/digest 不变，既有 capability 不自动获得 preview action，Core/plugin exact parity。
2. capability/lease：wrong DB/runtime/project/head/action/subject，expiry/revoke/restart，request proof replay/substitution，TTL/request/bytes/concurrency budgets。
3. media route：exact current clip、unknown/cross-project clip、source replacement、single/no/multi/invalid Range、redirect、MIME/size/ETag，第一字节前 fail-closed。
4. Browser：真实 video decode，Play/Pause/Seek/playhead/clip transition，console error 0，non-loopback request 0，URL/DOM/network 零 token/path/backend URL。
5. state honesty：playback 前后 Timeline/Usage/export ledger 不变，pending edit 仍须显式 Save。
6. full plugin/cache：final discovery、validator、cachebuster reinstall、source/cache parity、inventory/offline/whole-repository gate。

## Non-goals

- 不把 raw source preview 称为 final render/fidelity。
- 不实现 audio mix、subtitle、transition、crop、keyframe、multi-track、Split/Delete 或 Export。
- 不为 unsupported codec 伪造成功；derived transcode 是后续独立切片。
- 不为 Browser 暴露 filesystem path、backend URL 或任何 authority secret。
- 不用 playback 表明 Timeline edit 已保存或 source audio 已纳入 canonical mix。

## Promotion rule

next-schema authority、Core/plugin lease path、real browser playback、negative scope matrix、production inventory 和 final plugin/cache/repository gate 已在 2026-08-29 的同一冻结本地树上通过，因此本切片标为 `LOCALLY VALIDATED`。这不关闭父切片 B2B4 T053/T054；只有 fresh 真实 Codex↔DeepSeek model/host/UI 双向旅程才能消除该 promotion blocker。
