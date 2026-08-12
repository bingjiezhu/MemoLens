# MemoLens product strategy

## Product promise

MemoLens turns a private photo and video folder into a useful creative memory layer without requiring the user to surrender the library to a cloud media service.

The product is not trying to replace the file system or become another generic photo manager. Its advantage is the loop between **intent, evidence, curation, and reuse**:

```text
Connect library → understand it locally → express an intent → inspect why each asset fits → direct → edit → preview/save a verified copy or continue in Codex
```

## Primary users

1. **Private archivist** — has years of personal photos and videos and wants recall without cloud migration.
2. **Creative director** — has a rough video idea and needs grounded story options from a noisy personal library.
3. **Hands-on editor** — wants an editable first cut, not an opaque one-shot generation.
4. **AI power user** — wants the same memory layer available in Codex without wiring another model key.

## North-star outcome

> A new user reaches a trustworthy set of pictures and timestamped video moments, turns an idea into an editable first cut, and understands every source and privacy boundary along the way.

Supporting product measures:

- time to first indexed asset, first useful result, and first playable preview;
- successful searches that lead to basket additions, timeline edits, or a completed preview save;
- percentage of sessions that recover cleanly from offline, empty, partial, and cancelled states;
- duplicate suppression and result-diversity quality;
- percentage of timeline clips grounded in valid source ranges;
- zero unapproved disclosure of local paths, credentials, frames, audio, or original media.

## Experience principles

### One next action

The default interface follows a small state machine and presents one primary action:

```text
Starting → Service needed → Library needed → Indexing → Memories building → Ready
```

Provider routing, Python paths, database paths, and diagnostics remain available under Advanced Settings instead of competing with the first-run journey.

### Explain the selection

Every result should expose editable intent chips (time, place, people, exclusions, diversity, count) and a concise “why this asset” explanation. Video results point to a real `[start_ms, end_ms)` range. The user can request more like a result, replace a timeline clip, or exclude its pattern without rewriting the whole prompt.

### Memory, Director, Editor

The product is one connected loop, not three disconnected AI demos:

- **Memory** performs cheap local scanning first, stores timestamped video segments, and only revisits promising ranges at higher detail.
- **Director** translates audience, format, mood, duration, and must-have constraints into a grounded brief and storyboard.
- **Editor** applies typed, reversible operations to versioned timeline JSON and renders only validated revisions.

Natural language may propose an edit; it never becomes a shell command or an unvalidated FFmpeg filter graph.

### Local-first must be visible

Each operation communicates whether it is local or uses a configured provider, what leaves the machine, and whether a no-key fallback is available. Local-first is an interaction contract, not only a README claim.

In particular, an API-based photo vision profile receives a resized working copy of every photo it analyzes during indexing. Video frames and audio have a stricter, separate opt-in: merely configuring a provider key never authorizes them to leave the device. The interface must disclose the exact payload before indexing begins; it must never imply that an untouched original also means no media content leaves the device.

### Preserve before transforming

MemoLens never deletes or overwrites originals. Cleanup produces a review queue; previews live in an application cache; verified preview copies use a user-approved destination and fail rather than overwrite; failed indexing remains recoverable; Unicode metadata, source ranges, and user curation are preserved verbatim.

## Product sequence

### Foundation — shipped in 0.2

- verified backend identity and loopback boundaries;
- collision-safe indexing and explicit empty/partial/failure states;
- mobile and split-view layout correctness;
- cancellation, real request state, basket hydration, and accessible recovery actions;
- repository CI, security policy, and a Codex plugin that needs no separate model API key.

### Now — video creative workbench

- “Today in your memory” with forgotten highlights and recurring themes;
- editable intent chips and per-result explanations;
- adaptive video segmentation and timestamped mixed-media retrieval;
- drag-to-order storyboard, typed timeline revisions, local preview, and non-destructive Save As of the verified preview; final 1080p export remains gated on a scoped output grant;
- a safe-default Codex workflow for video search, draft creation, and offline validation;
- privacy/cost dashboard for local versus configured provider steps.

### Later — memory intelligence

- incremental/background indexing with removable-drive recovery;
- opt-in people and relationship clusters entirely on-device;
- deterministic evaluation sets for retrieval relevance, diversity, grounding, and privacy;
- optional adapters for additional agent surfaces while SQLite remains the source of truth.

## Non-goals

- silently reorganizing or deleting the user's media library;
- becoming a cloud sync or social publishing service;
- hiding provider costs or uploading the whole library for convenience;
- claiming frame-perfect understanding, replacing a professional NLE, or executing arbitrary model-generated FFmpeg commands.

These boundaries keep MemoLens focused: a private, inspectable bridge between a person's media memory and what they are trying to create.
