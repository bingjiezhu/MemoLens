# Implementation evidence: ML-015-B2B4B

## Current boundary

- Evidence date: 2026-08-29.
- Checkout: branch `codex/next-generation-creator-loop`, base HEAD `0caa2cf4afcb041150c131a41c4323a907dc6b6e`; evidence is from the current dirty shared worktree and is not a release or commit claim.
- Maturity: `IMPLEMENTED / LOCALLY VALIDATED / PARENT B2B4 REAL-HOST JOURNEYS PENDING`.
- Browser journey used an automated native-authority QA fixture with preview-only `timeline.preview_media`. It is explicitly not evidence of a real user's approval, final render fidelity, source audio, subtitles, transitions, export, remote CI, or a clean-machine install.

## Implemented chain

- Core mint: `POST /v1/agent/creative/projects/<project_id>/timeline/preview-leases`.
- Core media: project/head/clip/source-bound `GET|HEAD /v1/agent/preview-leases/<lease_id>/clips/<clip_id>/media` with independent HMAC request proof, nonce replay denial, definitive source/head/runtime recheck, MP4/H.264 allowlist, and bounded single-range streaming.
- Plugin proxy: cookie/origin/session/current-clip-bound `GET|HEAD /api/sessions/<session_id>/clip-media/<clip_id>`; Browser receives no lease secret, proof secret, backend URL, source path, asset/source identity, database UUID, or content digest.
- UI: one muted source viewer with Play/Pause, Timeline seek, source-in mapping, playhead, two-clip transition, representative JPEG fallback, visual Timeline, trim/reorder/duration/replace controls, append-only history, and explicit Save/Discard authority separation.

## Controlled-local proof on 2026-08-29

- Production inventory: 167 actions, 138 exact oracles, inventory SHA-256 `04ef7a6e481c8bc4cf8e573ca665da422bd7318773e511e5eef76a7a55803d79`, and `high_impact_oracle_gaps == {}`. Exact oracle runner passed 138/138.
- Core focused safe playback/authority suite passed 37/37 with warnings as errors.
- Plugin safe playback suite passed 11/11 with warnings as errors after the browser-found favicon correction.
- Production-wired fixture test passed 1/1 with warnings as errors. It uses production `create_app`, real loopback sockets, two distinct six-second `libx264`/`yuv420p`/`+faststart`/no-audio MP4 files, two committed JPEG representative keyframes, and Blueprint -> Coverage -> canonical Timeline construction.
- Real Codex in-app Browser loaded a 320x180 H.264 source with `readyState=4`, duration 6 seconds, `muted=true`, and no console warnings/errors. The boot fragment was removed before steady state and the rendered document contained none of the checked path, database, source, lease, proof, capability, or SHA fields.
- The eight-second journey moved from clip 1 to clip 2 and ended at `00:08.000`; Pause held the playhead; seeking to Timeline `00:05.000` selected clip 2 and mapped local `00:01.174` to source `00:02.174`.
- The browser asset inventory contained only the loopback bootstrap, two JPEG previews, and video proxy resources. A browser-discovered `/favicon.ico` 404 was corrected with an inline empty favicon and disappeared on the fresh rerun.
- Before and after playback, the Timeline, Usage, export, and capability-write ledger snapshot had the same canonical SHA-256 `11e1161d37864f5480b5beb676948cafc9da0e91c90b5c8734a8e80d7669a3d1`; `unchanged=true` and canonical Timeline remained revision 1.

## Screenshot evidence

- Viewer screenshot: `<TEMP_EVIDENCE_ROOT>`, 67,156 bytes, SHA-256 `072f50da0e8459d3e29e1b605911fcc145597b1fbb4e65b4e374fd20bfe6cb80`.
- Timeline screenshot: `<TEMP_EVIDENCE_ROOT>`, 79,744 bytes, SHA-256 `5b3c8a2742cc566469a7e662c7329e515c58b72bb6df93a718e51d541444f646`.
- Retained synthetic fixture: `<TEMP_EVIDENCE_ROOT>`. It contains only generated QA media/state and is not user-library evidence.

## Final frozen-tree and installed-plugin gate

- Gate directory: `<TEMP_EVIDENCE_ROOT>`.
- The gate froze base HEAD `0caa2cf4afcb041150c131a41c4323a907dc6b6e` with Git-visible tree SHA-256 `425f4cb642bb91dd37dc8ea3fdae4b0678d70d4e531cb96131e695d3f7894f6c` and porcelain status SHA-256 `08d3a101f19cf994750cd743a5e77768a0c72dd162335347e93b81738233771f`; the before/after tree and status byte streams were exact `cmp` matches.
- `npm run check` exited 0: Python **970/970** in `1032.417s`, plugin discovery **390/390** in `102.212s`, Node **162/162** in `659.349084ms`, and renderer **114/114** in `1166.684625ms`.
- Production inventory/oracle closure remained **167 actions / 138 exact negative oracles**. The final A0 gate log SHA-256 is `434707edb9543e3419cddd490c8268924731df4407da5d6347e37c1ca0d82488`.
- Codex reported the MemoLens plugin installed and enabled at `0.10.1+codex.20260830050929`; its cache is `<codex-cache>/memolens-local/memolens/0.10.1+codex.20260830050929`. Source and installed-cache validators both passed, and checksum dry-run source/cache parity emitted **0 bytes**.
- Final `git diff --check` emitted **0 bytes**. The `npm run check` log SHA-256 is `c5e42243a34ec7194ebabbe2298348923fa5eed55aa9ce57e570d534bc1c7ea7`; plugin-list evidence SHA-256 is `81b250d84f587145efa8809264fc732f3a9f71ef15e990e5ceb09cf1ad223679`.

## Residuals and non-claims

- Parent B2B4 T053/T054 remain open: no fresh real Codex→DeepSeek and DeepSeek→Codex model/host/UI journeys have yet demonstrated the same canonical project advancing N→N+1→N+2 without chat transfer. The process/browser fixtures and installed-plugin parity do not substitute for those journeys.
- The first browser profile intentionally supports only muted MP4/H.264 source preview. Other codecs, source audio, BGM, voiceover, mix, subtitles, transitions, final-fidelity render/export, remote CI, and clean-machine acceptance remain outside this proof.
- The worktree remains uncommitted and unreleased; local validation is not a release or production-readiness claim.
