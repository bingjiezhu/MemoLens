# Tasks: ML-015-B2B4B.1

## Contract

- [x] T001 Freeze transport/output/attestation as independent facts.
- [x] T002 Advance only `canonical_source_preview` to schema v2; retain lease v1, session v1, and SQLite V19.
- [x] T003 Remove `muted` and `source_audio_included`; add exact v2 policy fields.

## UI

- [x] T010 Enforce `controls=false`, `defaultMuted=true`, `muted=true`, and `volume=0` before load/play.
- [x] T011 Reapply the invariant on `volumechange` and fail closed if the v2 policy is absent or contradictory.
- [x] T012 Replace visible copy with explicit raw-transport, muted-output, no-attestation language.

## Fixture and tests

- [x] T020 Generate two H.264+AAC 48 kHz stereo faststart MP4 fixtures.
- [x] T021 Prove one video plus one AAC stream per source and exact source/proxy SHA-256 set equality.
- [x] T022 Cover v2 exact keys, removed v1 fields, UI mute guard, JavaScript syntax, Core playability, and adapter wording.
- [x] T023 Run focused suites with warnings as errors plus ruff and prompt/manifest syntax checks.

## Evidence boundary

- [x] T030 Record controlled-local results and preserve old B2B4B evidence as historical.
- [x] T031 Controlled-local Codex in-app Browser visually played the AAC-bearing fixture and observed `muted/defaultMuted=true` plus `controls=false`; no model call or native audio-device observation is implied.
- [ ] T032 Fresh DeepSeek Harness AAC Canonical Editor journey and DeepSeek model call with the same fixture. Fresh loader/plugin inventory alone does not satisfy this task.
- [ ] T033 Remote CI, clean-machine acceptance, release, and real user/native audio-device observation.
