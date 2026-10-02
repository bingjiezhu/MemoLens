# Feature Specification: Verified Visual Replacement Review

- Feature ID：`ML-015-B2B4A`
- 创建日期：2026-08-29
- 实施状态：`IMPLEMENTED / LOCALLY VALIDATED / PARENT B2B4 PROMOTION BLOCKED`
- 发布状态：`NOT COMMITTED / NOT RELEASED`
- 父切片：[ML-015-B2B4 Codex / DeepSeek Canonical Editor Handoff](../015-b2b4-codex-deepseek-canonical-editor-handoff/spec.md)
- donor 边界：[editor-donor-adoption-2026-08-29.md](../015-b2b4-codex-deepseek-canonical-editor-handoff/editor-donor-adoption-2026-08-29.md)

## Objective

把 canonical editor 的 `replace_clip` 从只显示 ID 的下拉框升级为可复核的“当前画面 vs 同一 Beat 已验候选”界面，同时保持 B2B3/B2B4 已冻结的单一 Timeline 真源、closed edit、Stage/Save 分离和 exact CAS。

本切片解决的是 **visual choice review**，其 representative JPEG 本身不是媒体播放、最终画质预览或新剪辑方言。后续 [ML-015-B2B4B](../015-b2b4b-safe-canonical-playback-grant/spec.md) 以独立 `timeline.preview_media` 授权提供受限、静音的 verified source playback；它不改变本切片的 thumbnail 证据边界。

## Authority flow

```text
current server-held Timeline clip
  + same-Beat Coverage alternative
  + verified evidence proof and digest
  → candidate card + representative JPEG
  → Browser submits clip_id + assignment_id only
  → Stage replace_clip (process memory, noncanonical)
  → explicit Save
  → Core re-resolves assignment/source + exact head CAS
  → canonical reread
```

- Browser 不接收或提交 asset path、source locator、asset SHA、evidence document、proof 或 replacement Timeline。
- candidate eligibility 由 session 开启时的 server-derived snapshot 决定；HTTP manager 先验证 clip-scoped candidate，paired backend 再验证同 Beat alternative、verified proof、proof digest、asset identity 和可用时长。
- thumbnail adapter 只通过现有 backend verified media route 获取有界 JPEG bytes；浏览器没有 Desktop token 或 pairing secret。
- thumbnail 加载成功不授予写权限。候选在 Save 前发生变化时，Core 必须按 B2B4 stale/CAS 规则拒绝。

## Functional requirements

- **FR-B2B4A-001**：每个可编辑 clip 显示当前 canonical 卡和同一 Beat 的 eligible replacement 卡；候选为空时显示诚实空状态。
- **FR-B2B4A-002**：候选必须来自 server-derived `replacement_candidates[clip_id]`，且保持 `assignment_id/evidence_ref/media_kind/asset_id/asset_sha256/current_slot_duration_ms/available_duration_ms` 的 closed projection。
- **FR-B2B4A-003**：`GET /api/sessions/{session_id}/replacement-previews/{clip_id}/{assignment_id}` 必须 loopback、same-origin、cookie-bound、`no-store`、bounded JPEG only。
- **FR-B2B4A-004**：路由必须在 backend media read 前拒绝 unknown clip、unknown assignment、cross-clip/cross-Beat assignment、失效 proof、错 digest、短于当前 slot 的 video span 和 unsupported media kind。
- **FR-B2B4A-005**：页面只能提交 `replace_clip {clip_id, assignment_id}`；不得提交 card 内展示的 asset/evidence/source 字段。
- **FR-B2B4A-006**：点击候选只 Stage pending；页面必须继续显示 canonical N 仍是 authority，并保留 Save/Discard。
- **FR-B2B4A-007**：Save、idempotency、receipt、operation、head CAS 和 canonical reread 继续使用 B2B4 既有链，不新增 UI-owned write path。
- **FR-B2B4A-008**：缩略图失败时保留文字 identity/eligibility fallback，不把 broken image 当作 eligibility failure，也不把 fallback 伪装成已看见画面。
- **FR-B2B4A-009**：production inventory 必须在双向 route discovery 中声明新 GET action，并用 exact negative oracle 绑定 session/origin 与 clip/assignment scope refusal。
- **FR-B2B4A-010**：不得仅凭本切片的 representative-card/JPEG 证据声称 playback、final fidelity、任意视频帧选择、render/export 或 Split/Delete/Duplicate/Transition 已实现；playback 只能引用 B2B4B 的独立授权与验证。

## Stable errors

| Code | Meaning |
| --- | --- |
| `canonical_editor_replacement_unavailable` | candidate 不在当前 clip 的同 Beat eligible set，proof/source 失效，或 verified thumbnail 不可用 |
| `editor_session_unauthorized` | 缺失或错误 session cookie |
| `editor_origin_invalid` | 非同源请求 |
| `editor_route_not_found` | 路由形状、session kind 或 identity segment 不受支持 |

## Required validation

1. unit：image/video candidate proof derivation、cross-Beat/unknown/proof tamper、cookie/origin、bounded bytes、Stage-only 和 page static assertions。
2. inventory：discovered route 与 frozen action 双向相等，oracle exact selector 单一通过，high-impact gaps 保持空。
3. browser：真实页面显示 current/eligible 两列，JPEG 解码、Stage 后 pending、console error 0、non-loopback request 0、URL/DOM 不含 boot token/绝对路径/authority secret。
4. full plugin/cache：最终 shared diff 上完整 discovery、validator、cachebuster reinstall 和 source/cache parity；这不替代真实 Codex/DeepSeek model journey。

## Non-claims

- representative thumbnail 不是 playback、时间精确视频帧或 final-fidelity preview。
- B2B4B 已独立实现受 `timeline.preview_media` 约束的 muted MP4/H.264 verified source preview；该证据不能回填为 B2B4A thumbnail 本身的播放证明。
- 当前只支持 B2B3 已有的 `replace_clip`；没有因为参考 OpenCut/OpenChatCut/ChatCut 而引入 donor state 或代码。
- 本切片不关闭 B2B4 T053/T054 的 fresh 双向 real-model/host journey blocker。
