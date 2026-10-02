# Implementation Plan: ML-015-B2B4B

## Status

- 当前：`IMPLEMENTED / LOCALLY VALIDATED / PARENT B2B4 REAL-HOST JOURNEYS PENDING`
- Phase 1–5 已按“先 authority + negative oracles，再 media proxy，最后 UI/playhead 与真实浏览器验收”完成并通过最终本地门禁。
- 禁止先把旧 Desktop-token raw route 放宽，再补权限。
- 父切片 B2B4 T053/T054 的 fresh Codex↔DeepSeek model/host/UI 双向旅程仍未完成，不由本切片的 fixture/Browser 证据替代。

## Phase 1 — Frozen red contract

1. 冻结 `timeline.preview_media`、lease mint/result、editor proxy 与 backend Range contract。
2. 添加证明当前 `media_playback=false`、Browser 无 authority、旧 raw route 需 Desktop token 的 red oracles。
3. 把新 routes/actions 先加入 production inventory 的 expected surface 与 negative oracle 计划，不用 wildcard selector。

## Phase 2 — Additive paired read authority

1. 在 next managed-schema migration 中添加 `timeline.preview_media` closed action，保存历史行/字节/digest/event sequence。
2. Core 和 standalone plugin 同时更新 exact version/name/checksum/physical manifest/future refusal。
3. 更新 pairing presentation/approval/revoke/credential allowlist，明确 preview read 不是 edit/render/export/file-system grant。
4. 实现 bounded in-memory PreviewLeaseBroker；进程重启即失效，每次 read 再验 definitive capability/head/source。

## Phase 3 — Project/clip-bound media data plane

1. lease mint 只接受 exact project/head/nonce，Core 自行导出 clip/source whitelist。
2. 新建 Agent preview media route，复用 verified source opener 与 Range parser，但不复用 Desktop-token authority。
3. 对 Range/MIME/bytes/TTL/request/concurrency 做 closed budgets，禁止 redirect、多 Range、path/URL input。
4. source/head/capability 变化时在 response headers/body 前失败，不继续旧 handle。

## Phase 4 — Editor proxy and playback UI

1. canonical handoff 只在 credential 含 preview action 时获取 lease；无 action 时保留 thumbnail-only 诚实降级。
2. editor server 新建 cookie/origin/session/clip-bound `GET|HEAD clip-media` proxy，流式转发单 Range，不缓冲整个文件。
3. UI 增加单一 active media viewer、Play/Pause、seek/scrub、clip transition 与 unsupported fallback；source preview 默认 muted。
4. 将 pending trim/duration 投影到本地播放窗口，但不写 canonical state；Save 后以 exact reread 重建 lease/session。

## Phase 5 — Evidence and promotion

1. 运行 migration/parity/capability/lease/media/proxy/UI 正负矩阵。
2. 使用真实浏览器解码本地视频，保存 screenshot/console/network/range/token/path evidence。
3. 检查 Timeline/Usage/export ledger 在播放前后零变化。
4. 更新 production inventory/exact oracles，运行 full plugin discovery/validator/cache reinstall/parity 与 whole-repository gate。

## Rollback

- 关闭 lease mint 或 preview UI 时，canonical editor 回到 verified thumbnail-only；Timeline 不变。
- 不回退 managed schema，不删除历史 capability/receipt/event。
- 不通过恢复全库 Desktop-token proxy 作为 rollback。
