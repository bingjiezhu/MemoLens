# Requirements Checklist: ML-015-B2B4B.1

- Implementation status: `IMPLEMENTED / VALIDATED_CONTROLLED_LOCAL`
- Release status: `NOT COMMITTED / NOT RELEASED`

## Truth contract and authority

- [x] Raw MP4 transport is declared as possibly containing audio; no source-audio absence is inferred from H.264 admission.
- [x] `memolens.canonical_source_preview@2` separates muted output, disabled audio playback, possible audio-bearing transport, no stream attestation, and no final-fidelity claim.
- [x] Preview lease/session remains v1 and SQLite remains V19; the correction does not widen source/head/capability/budget authority.
- [x] Canonical Timeline, Usage, Export, and capability ledgers remain unchanged during preview.

## UI and byte transport

- [x] The page exposes no native controls and enforces `defaultMuted=true`, `muted=true`, and `volume=0` in code, including a `volumechange` guard.
- [x] Two H.264+AAC 48 kHz stereo faststart fixtures contain one H.264 and one AAC stream each.
- [x] Full-range same-origin proxy payload SHA-256 equals the admitted source bytes for both fixtures.
- [x] Controlled-local Codex Browser visually played the AAC transport and exposed `muted/defaultMuted=true` plus `controls=false`.
- [ ] Native user/audio-device acceptance observes actual speaker silence. Browser attributes and static code do not substitute for this.

## Host and model promotion

- [x] Fresh Codex cachebuster install, source↔cache parity, and installed-cache subset tests passed.
- [x] Official DeepSeek Harness fresh profile mounted the shared bundle and started the MCP child from the installed cache.
- [ ] A Codex model tool call opens and operates the AAC Canonical Editor in a fresh task that loads this plugin version.
- [ ] A DeepSeek model call opens and operates the AAC Canonical Editor in a fresh profile.
- [ ] DeepSeek AAC Browser playback independently observes the same transport/output boundary.

## Repository and release

- [x] Focused warning-as-error suites passed 77/77 and production negative oracle passed 141/141.
- [x] First full local `npm run check` passed on its then-current diff; final current-diff rerun remains a root-task backfill item.
- [ ] Remote CI and clean-machine acceptance pass.
- [ ] Commit, tag, release, and supported-host production acceptance complete.

Unchecked items are promotion residuals. They do not invalidate the narrower controlled-local truth correction, but they prevent claims of real model-driven, native-audio, released, or production-ready playback.
