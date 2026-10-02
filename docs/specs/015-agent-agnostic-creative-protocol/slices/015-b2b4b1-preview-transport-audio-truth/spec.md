# Feature Specification: Preview Transport Audio Truth Correction

- Feature ID: `ML-015-B2B4B.1`
- Date: 2026-08-30
- Implementation status: `IMPLEMENTED / VALIDATED_CONTROLLED_LOCAL`
- Release status: `NOT COMMITTED / NOT RELEASED`
- Parent: [ML-015-B2B4B Safe Canonical Source Playback Grant](../015-b2b4b-safe-canonical-playback-grant/spec.md)
- Evidence: [implementation-evidence.md](implementation-evidence.md)
- Checklist: [checklists/requirements.md](checklists/requirements.md)
- Priority: P0 truth and safety correction

## Problem

B2B4B correctly constrained preview bytes to the current project/head/clip and kept the browser viewer muted, but its first QA fixture generated video-only MP4 and the browser projection asserted `source_audio_included=false`. The proxy does not inspect or strip audio streams; it forwards the exact admitted source bytes. Therefore that field claimed knowledge the system did not possess and would become false for an otherwise playable H.264 MP4 carrying AAC.

This correction keeps the useful source-preview path while separating three facts:

1. the raw MP4 transport may contain an audio stream;
2. the page disables audio playback and keeps output muted; and
3. MemoLens does not attest audio-stream presence, content, mix, or final fidelity.

## Frozen boundary

```text
timeline.preview_media lease v1 (unchanged)
  -> exact admitted MP4 bytes, which may contain audio
  -> canonical_source_preview browser projection v2
  -> page validates the v2 policy
  -> defaultMuted=true + muted=true + volume=0
  -> volumechange re-applies the mute invariant
  -> no audio-stream or final-fidelity attestation
```

- SQLite remains V19; this slice has no database migration.
- Core preview lease/request/read schemas remain v1; source/head/capability/budget admission is unchanged.
- The canonical handoff/session remains v1. Only the nested `memolens.canonical_source_preview` projection advances to v2.
- The proxy remains byte-preserving. It does not demux, transcode, strip, decode, analyze, or attest audio.

## Browser playback projection v2

The browser playback object must include the existing identity, mode, clip availability, and reason fields plus these exact policy facts:

```json
{
  "object": "memolens.canonical_source_preview",
  "schema_version": "2",
  "output_muted": true,
  "audio_playback_enabled": false,
  "transport_may_include_audio": true,
  "audio_stream_attested": false,
  "final_fidelity": false
}
```

The v1 fields `muted` and `source_audio_included` are removed. `output_muted` describes the page output policy, not the bytes. `transport_may_include_audio` is deliberately conservative and does not assert that every source has audio.

## UI invariant

- The `<video>` element starts with the HTML `muted` attribute and exposes no native controls.
- Before load and before play, the page sets `controls=false`, `defaultMuted=true`, `muted=true`, and `volume=0`.
- A `volumechange` listener reapplies the same invariant.
- Playback fails closed to the representative thumbnail unless all v2 policy facts match exactly.
- Visible copy states: raw MP4 may contain audio; playback is disabled and output muted; presence/content/mix are not attested; preview is not final-fidelity proof.

## Functional requirements

- **FR-B2B4B1-001**: never infer `source_audio_included=false` from an MP4/H.264 playback admission or lease.
- **FR-B2B4B1-002**: emit `memolens.canonical_source_preview@2` with the five exact policy facts above; omit the two misleading v1 fields.
- **FR-B2B4B1-003**: keep browser audio playback disabled and output muted across initial state, load, play, and volume changes; expose no unmute/volume control.
- **FR-B2B4B1-004**: keep raw proxy bytes exact. An admitted H.264+AAC fixture must remain byte-identical after the same-origin proxy.
- **FR-B2B4B1-005**: keep preview lease v1, canonical handoff/session v1, SQLite V19, authority, budget, nonce, source/head, and ledger behavior unchanged.
- **FR-B2B4B1-006**: Codex skill, DeepSeek skill/prompt, MCP description, manifest, and READMEs must use the same transport/output/attestation language.
- **FR-B2B4B1-007**: do not claim source-audio editing, audible playback, transcription, analysis, BGM, voiceover, mixing, render, export, or final fidelity.

## Required validation

1. Core fake probe includes H.264 plus AAC metadata and remains admitted as playable; audio metadata must not widen lease output or authority.
2. Production-wired QA creates two `libx264 + AAC`, 48 kHz, stereo, faststart MP4 files; ffprobe must observe one H.264 and one AAC stream per file.
3. Full-range browser proxy bytes must hash exactly to the two source MP4 byte streams.
4. Browser state must expose only playback v2 facts and must omit `muted` and `source_audio_included`.
5. Static UI tests must bind policy-v2 validation, pre-play mute enforcement, and the `volumechange` guard; JavaScript syntax must pass.
6. Plugin discovery and DeepSeek prompt tests must reject wording drift.

## Non-goals and promotion boundary

This slice proves an honest audio-bearing transport fixture and a code-enforced muted-output policy in controlled local tests. A controlled-local Codex in-app Browser also visually decoded and played that AAC-bearing transport while exposing `muted/defaultMuted=true` and `controls=false`; its element proxy did not expose `volume`, so only static/code tests cover the `volume=0` assignment.

That journey was not initiated by a Codex model tool call and did not include native user/audio-device observation. The official DeepSeek fresh profile proved loader/plugin inventory only; it had no API key/model call and did not run the AAC editor. This slice therefore does not prove DeepSeek AAC playback, OS/device audio silence, adversarial DevTools resistance, audio-stream inspection, audio-content correctness, a canonical mix, final render fidelity, Remote CI, clean-machine acceptance, release, or production readiness.
