# Implementation evidence: ML-015-B2B4B.1

## Current boundary

- Evidence date: 2026-08-30.
- Checkout: branch `codex/next-generation-creator-loop`, base HEAD `0caa2cf4afcb041150c131a41c4323a907dc6b6e`; evidence comes from the current dirty shared worktree.
- Maturity: `IMPLEMENTED / VALIDATED_CONTROLLED_LOCAL`.
- Installed cache: `0.10.1+codex.20260830121850`; source↔cache checksum parity is byte-exact. The slice remains not committed, pushed, tagged, or released.

## Implemented correction

- `memolens.canonical_source_preview` advances from v1 to v2 while the canonical editor handoff/session, preview lease, and read requests remain v1 and SQLite remains V19.
- The projection now distinguishes `output_muted=true`, `audio_playback_enabled=false`, `transport_may_include_audio=true`, `audio_stream_attested=false`, and `final_fidelity=false`.
- The page validates all five facts before playing, sets `controls=false`, `defaultMuted=true`, `muted=true`, and `volume=0`, and reapplies the invariant on `volumechange`.
- The production-wired fixture generates two six-second H.264/yuv420p plus AAC/48 kHz/stereo faststart MP4 sources. Its test observes one H.264 and one AAC stream per file and verifies the two full-range proxy payload hashes equal the two source-file hashes.
- Codex skill/README/manifest, DeepSeek skill/prompt/README, and MCP tool description now say the raw MP4 transport may contain audio while browser audio playback is disabled and output muted; no audio-stream or final-fidelity attestation is implied.

## Focused validation

All Python suites below ran with `-W error`:

| Surface | Command | Result |
| --- | --- | --- |
| Core preview authority/data plane | `.venv/bin/python -W error -m unittest tests.test_b2b4b_safe_playback` | 5/5 in 8.393s |
| Production-wired AAC fixture/proxy | `.venv/bin/python -W error -m unittest tests.test_b2b4b_playback_qa_fixture` | 1/1 in 3.510s |
| Plugin playback/session/UI | `.venv/bin/python -W error -m unittest discover -s .agents/plugins/plugins/memolens/tests -p 'test_safe_canonical_playback.py'` | 11/11 in 3.705s |
| Canonical Editor regression | `.venv/bin/python -W error -m unittest discover -s .agents/plugins/plugins/memolens/tests -p 'test_canonical_editor_handoff.py'` | 38/38 in 14.492s |
| DeepSeek adapter/prompt | `.venv/bin/python -W error -m unittest discover -s .agents/plugins/plugins/memolens/tests -p 'test_deepseek_harness.py'` | 6/6 in 0.118s |
| Plugin discovery/manifest | `.venv/bin/python -W error -m unittest discover -s .agents/plugins/plugins/memolens/tests -p 'test_plugin.py'` | 16/16 in 8.187s |

Total focused tests: 77/77. Ruff passed for every changed Python file. `node --check` passed for `prompt.js`, the embedded Canonical Editor script passed through its focused UI test, and the plugin manifest parsed as JSON.

## Installed-cache, Browser, and loader validation

- The installed cache at `<codex-cache>/memolens-local/memolens/0.10.1+codex.20260830121850` matched the source plugin by checksum. With the source repository on `PYTHONPATH`, its safe-playback, Canonical Editor, and DeepSeek suites passed 55/55. This proves the installed byte path, not a Codex model tool call.
- The controlled-local Codex in-app Browser loaded the production-wired H.264+AAC fixture and displayed the actual video. After Play, the UI playhead advanced from 0 to about 1.021s and the source element reached about 2.214s. The element proxy exposed `muted=true`, `defaultMuted=true`, and `controls=false`; it did not expose `volume`, so runtime `volume=0` is covered only by static/code tests and is not claimed as Browser-observed.
- The Browser journey remained preview-only: canonical Timeline had four rows, Usage and Export remained zero, the three capability rows were unchanged, the canonical baseline/current SHA stayed `c96b894...cc406`, and the head stayed N1. The fixture shut down cleanly without retaining its temporary directory.
- The official DeepSeek Harness fresh bundled profile mounted and enabled `memolens-bundle-root`, `memolens-mcp`, `memolens-skill`, and `memolens/prompt`; shutdown output showed the MCP child had started from the installed cache. No API key or model call was available, and no DeepSeek AAC Browser journey ran.

## Broader local gates

| Gate | Observed result | Status |
| --- | --- | --- |
| Production negative oracle after this correction | 141/141 (`failed=0`, `skipped=0`) | PASSED |
| First full `npm run check` on its then-current diff | Core 1043/1043; plugin discovery 432 tests passed; renderer-model 120/120; exit 0 | PASSED |
| Final current-diff full repository rerun after all later code merges | Not yet backfilled in this snapshot | PENDING ROOT TASK |

## Preserved historical evidence

The original B2B4B evidence accurately records what its 2026-08-29 fixture ran: video-only MP4 files and a muted real Codex browser journey. This successor does not edit that evidence. It records why the earlier `source_audio_included=false` projection was not a valid general transport claim and replaces it prospectively.

## Residuals and non-claims

- A controlled-local Codex Browser decoded and visually played the AAC-bearing transport, but no Codex model tool call initiated that journey and no native user/audio-device acceptance observed actual speaker silence.
- DeepSeek completed fresh loader/UI plugin inventory only. It did not run an AAC Canonical Editor journey or a real model call.
- Static/browser-code enforcement plus Browser-observed mute attributes does not prove OS/device audio silence against adversarial DevTools or a compromised page.
- No component attests whether an arbitrary source has an audio stream, what it contains, whether it is synchronized, or how it should be mixed.
- Source-audio editing, audible playback, transcription, BGM, voiceover, mixing, subtitle, transition, render/export fidelity, Remote CI, clean-machine acceptance, commit, and release remain unverified by this slice.
