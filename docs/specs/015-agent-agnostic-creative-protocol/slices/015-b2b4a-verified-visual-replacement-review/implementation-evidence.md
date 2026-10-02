# Implementation Evidence: ML-015-B2B4A

- 证据日期：2026-08-29
- checkout：shared dirty worktree，branch `codex/next-generation-creator-loop`
- base HEAD：`0caa2cf4afcb041150c131a41c4323a907dc6b6e`
- 当前状态：`IMPLEMENTED / LOCALLY VALIDATED / PARENT B2B4 PROMOTION BLOCKED`
- Remote CI / release：`NOT RUN / NOT RELEASED`

## Current-disk implementation

- `memolens_canonical_editor.py`：从 server-held snapshot 推导候选，按同 Beat alternative + verified proof/digest 再验 image asset 或 video span，并只向 backend 请求 verified representative JPEG。
- `memolens_editor_server.py`：新增 `GET /api/sessions/<session_id>/replacement-previews/<clip_id>/<assignment_id>`；cookie/origin/session/clip/assignment/bounded-byte fail-closed。
- `ui/canonical-editor.html`：current-vs-eligible cards、真实图片元素、文字 fallback 与 `Stage this replacement`；没有新增写路径。
- `test_canonical_editor_handoff.py`：覆盖路由 security/scope、image/video derivation、proof tamper、UI static contract。
- production inventory：B2B4A focused 快照当时新增一个 action 与一个 exact oracle，历史值为 **163 actions / 128 negative oracles**，hash `2089b01a2f8f54fdcb744e5a3eab3dd51abd2afb7ef487170e662a6fafcd5bf1`。这不是最终组合 diff 的 current 计数。

## Verification observed

- canonical editor handoff：**21/21 passed in 9.312s**。
- production surface inventory：**20/20 passed in 0.988s**。
- focused Ruff：passed。
- focused `git diff --check`：passed。
- fresh 真实浏览器：isolated V14 database + production Flask authority + real paired backend + production canonical editor handoff；console error **0**，non-loopback URL **0**。
- 页面显示 **2** 组 `Current canonical / Eligible replacement` 对照卡；**6** 个初始图片节点均真实解码为 `640×360`。
- 点击候选后页面显示 `Pending · not canonical`；Core 冷读仍是 revision 1 与原 assignment，证明 Stage 没有写 canonical state。
- 点击 `Save as N2` 后，页面显示 `Saved revision N2 after exact canonical reread.`；Core 冷读确认 revision `1 → 2`，只有第一段 assignment/asset 换成选中候选，第二段不变。
- replacement preview 拒绝矩阵：正常请求 `200 image/jpeg`；无 cookie `401 editor_session_unauthorized`；cross-origin `403 editor_origin_invalid`；unknown assignment、unknown clip 和 cross-Beat assignment 均 `404`。所有响应保持 `no-store`、`same-origin` 和 `nosniff`。
- screenshot：`<TEMP_EVIDENCE_ROOT>`，`1280×720`，SHA-256 `a32ea9fc6b8df5d3e62e492349a467683ea62a07af2021c70688ba928c8ebf55`。主任务已重新读取文件并复核 digest/尺寸。

## Final frozen-tree gate

- 门禁日期：2026-08-29；目录：`<TEMP_EVIDENCE_ROOT>`。
- base HEAD：`0caa2cf4afcb041150c131a41c4323a907dc6b6e`。冻结 Git-visible tree SHA-256 为 `425f4cb642bb91dd37dc8ea3fdae4b0678d70d4e531cb96131e695d3f7894f6c`，porcelain status SHA-256 为 `08d3a101f19cf994750cd743a5e77768a0c72dd162335347e93b81738233771f`；门禁前后 tree/status 字节流 exact `cmp` 相等。
- `npm run check` exit 0：Python **970/970**（`1032.417s`），plugin discovery **390/390**（`102.212s`），Node **162/162**（`659.349084ms`），renderer **114/114**（`1166.684625ms`）。
- 最终组合 inventory 为 **167 actions / 138 exact negative oracles**；A0/final oracle gate 全绿，不以 B2B4A 的历史 `163/128` 快照冒充 current 值。
- Codex plugin 已安装并 enabled，版本 `0.10.1+codex.20260830050929`，cache 为 `<codex-cache>/memolens-local/memolens/0.10.1+codex.20260830050929`。source 和 cache validator 均通过；checksum dry-run parity 输出 **0 bytes**。
- 最终 `git diff --check` 输出 **0 bytes**。`npm run check` log SHA-256 为 `c5e42243a34ec7194ebabbe2298348923fa5eed55aa9ce57e570d534bc1c7ea7`，A0 log SHA-256 为 `434707edb9543e3419cddd490c8268924731df4407da5d6347e37c1ca0d82488`，plugin list SHA-256 为 `81b250d84f587145efa8809264fc732f3a9f71ef15e990e5ceb09cf1ad223679`。

## Residuals and non-claims

- B2B4 T053/T054 的 fresh 双向 real-model/host edit journey 未完成；本切片没有将 unit/browser fixture 伪装成该证据。
- representative JPEG 不证明 playback、arbitrary-frame seek、source audio、render/export 或 final fidelity。B2B4B 以独立 `timeline.preview_media` 授权验证了受限的 muted MP4/H.264 source playback，但这不改变 B2B4A thumbnail 的证据边界。
- 配对审批使用临时自动化 QA fixture，不等于真实用户原生审批，也不证明模型调用。
