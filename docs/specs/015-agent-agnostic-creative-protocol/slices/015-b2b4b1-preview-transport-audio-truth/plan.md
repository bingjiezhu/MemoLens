# Plan: ML-015-B2B4B.1

- Status: `IMPLEMENTED / VALIDATED_CONTROLLED_LOCAL`

## Decision

Correct the browser-facing truth contract without changing the preview authority or persistence layers.

## Sequence

1. Replace the false v1 audio absence assertion with a self-versioned playback v2 policy projection.
2. Make the page enforce muted output independently of transport contents and fail closed on policy drift.
3. Upgrade the production-wired fixture from video-only H.264 to H.264+AAC while preserving faststart and byte-exact proxying.
4. Align Codex, DeepSeek, MCP, manifest, and README language.
5. Run focused Core/plugin/fixture/manifest tests with warnings as errors and record residuals.
6. Install a fresh Codex cache version, verify source↔cache parity, and run the AAC fixture through the controlled-local in-app Browser without widening authority or canonical ledgers.
7. Mount the same installed bundle in an official DeepSeek Harness fresh profile; record loader evidence separately from the still-pending DeepSeek AAC/model journey.
8. Record the passing first full local repository gate, while reserving final current-diff counts for the root task after all later code merges.

## Compatibility

- No lease schema migration: lease v1 is an authority contract and does not attest audio.
- No SQLite migration: V19 already contains every durable fact this slice needs.
- No handoff/session migration: the nested playback object is independently versioned.
- Existing B2B4B evidence stays historical. This successor records the correction instead of rewriting the earlier no-audio fixture claim.
