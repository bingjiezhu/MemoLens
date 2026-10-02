# Tasks: ML-015-B2B4B

> 当前状态：`IMPLEMENTED / LOCALLY VALIDATED / PARENT B2B4 REAL-HOST JOURNEYS PENDING`。真实浏览器、production inventory、plugin/cache parity 与 final repository gate 已通过；父切片 B2B4 T053/T054 仍需真实双向 host/model/UI 旅程。

## Contract and red baseline

- [x] T001 冻结 `timeline.preview_media`、lease/proof/budget、backend media 和 editor proxy closed contracts。
- [x] T002 添加当前 `media_playback=false`、旧 raw route Desktop-token-only、Browser 无 secret/path/backend URL 的 red oracles。
- [x] T003 冻结 exact inventory actions/oracles 与 stable errors，覆盖 mint/read/proxy 三层。

## Additive authority

- [x] T010 以 next additive migration 扩展 closed capability union，不改写既有 capability/receipt/event bytes 或 digests。
- [x] T011 同步 Core/plugin exact schema name/version/checksum/manifest/future refusal 与 cold audit。
- [x] T012 更新 native pairing presentation/approval/revoke/credential action allowlist；既有 capability 不自动获得 preview。
- [x] T013 实现 short-lived server-held lease broker 与 request proof，restart/expiry/revoke/runtime epoch 使 lease 失效。

## Media data plane

- [x] T020 lease mint 仅接受 project/head/nonce；Core 导出 current clip/source whitelist 和 exact budgets。
- [x] T021 实现 project/head/clip/source-bound Agent media `GET|HEAD`，每读 definitive recheck，不放宽旧 raw route。
- [x] T022 复用 verified source opener/single-Range streamer，封闭 MIME，限制 bytes/requests/concurrency/TTL，禁止 redirect/multi-range。
- [x] T023 覆盖 wrong DB/runtime/project/head/action/clip/source、replacement、expiry/revoke/restart/budget/range 失败矩阵。

## Canonical editor

- [x] T030 canonical handoff 只在明确 preview capability 下获取 lease；无 capability 诚实保持 thumbnail-only。
- [x] T031 新建 cookie/origin/session/clip-bound `GET|HEAD clip-media` proxy，Browser 零 authority secret/path/backend URL。
- [x] T032 UI 实现 active viewer、Play/Pause/seek/playhead/clip transition 和 unsupported fallback，source preview 默认 muted。
- [x] T033 pending trim/duration 只影响 noncanonical local playback window；Save/Discard/CAS/reread 链不变。

## Verification and promotion

- [x] T040 更新 production inventory 双向 discovery 和 exact negative oracles，`high_impact_oracle_gaps == {}`。
- [x] T041 运行真实浏览器 decode/Play/Pause/Seek/console/network/range/token/path QA，保存 screenshot digest。
- [x] T042 证明 playback 前后 Timeline/Usage/export/capability-write ledger 不变，不声称 final fidelity 或 source-audio mix。
- [x] T043 在 final frozen tree 运行 full plugin discovery、source/cache validator、cachebuster reinstall/source-cache exact parity、inventory/offline/whole-repository gate。
- [x] T044 回填 exact checkout/tree+status digest、counts/runtime、plugin version/cache parity、screenshot 与 residuals；本切片按同次全绿证据 locally validated。
