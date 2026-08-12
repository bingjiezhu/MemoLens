# MemoLens product strategy

## Product promise

MemoLens turns a private photo folder into a useful memory layer without requiring the user to surrender the library to a cloud photo service.

The product is not trying to replace the file system or become another generic photo manager. Its advantage is the loop between **intent, evidence, curation, and reuse**:

```text
Connect library → understand it locally → express an intent → inspect why each photo fits → curate → export or continue in Codex
```

## Primary users

1. **Private archivist** — has years of personal photos and wants recall without cloud migration.
2. **Creative curator** — needs a coherent set, caption, or story from a noisy library.
3. **AI power user** — wants the same memory layer available in Codex or chat workflows without wiring another model key.

## North-star outcome

> A new user reaches a trustworthy, useful set of photos from an existing folder in under five minutes, understands why the photos were selected, and can continue refining the result without leaving the local-first boundary.

Supporting product measures:

- time to first indexed photo and time to first useful set;
- successful searches that lead to basket additions or export;
- percentage of sessions that recover cleanly from offline, empty, partial, and cancelled states;
- duplicate suppression and result-diversity quality;
- zero unapproved disclosure of local paths, credentials, or raw photos.

## Experience principles

### One next action

The default interface follows a small state machine and presents one primary action:

```text
Starting → Service needed → Library needed → Indexing → Memories building → Ready
```

Provider routing, Python paths, database paths, and diagnostics remain available under Advanced Settings instead of competing with the first-run journey.

### Explain the selection

Every generated set should expose editable intent chips (time, place, people, exclusions, diversity, count) and a concise “why this photo” explanation. The user can request more like a result or exclude its pattern without rewriting the whole prompt.

### Local-first must be visible

Each operation communicates whether it is local or uses a configured provider, what leaves the machine, and whether a no-key fallback is available. Local-first is an interaction contract, not only a README claim.

In particular, an API-based vision profile receives a resized working copy of every photo it analyzes during indexing. The interface must disclose that before indexing begins; it must never imply that an untouched original also means no image content leaves the device.

### Preserve before transforming

MemoLens never deletes or overwrites originals. Cleanup produces a review queue; exports are copies or manifests; failed indexing remains recoverable; Unicode metadata and user curation are preserved verbatim.

## Product sequence

### Now — trustworthy foundation

- verified backend identity and loopback boundaries;
- collision-safe indexing and explicit empty/partial/failure states;
- mobile and split-view layout correctness;
- cancellation, real request state, basket hydration, and accessible recovery actions;
- repository CI, security policy, and a Codex plugin that needs no separate model API key.

### Next — the delightful loop

- “Today in your memory” with forgotten highlights and recurring themes;
- editable intent chips and per-result explanations;
- drag-to-order story board, cover/crop preview, caption variants, and non-destructive export;
- privacy/cost dashboard for local versus configured provider steps.

### Later — memory intelligence

- incremental/background indexing with removable-drive recovery;
- opt-in people and relationship clusters entirely on-device;
- deterministic evaluation sets for retrieval relevance, diversity, and grounded copy;
- optional adapters for additional agent surfaces while SQLite remains the source of truth.

## Non-goals

- silently reorganizing or deleting the user's photo library;
- becoming a cloud sync or social publishing service;
- hiding provider costs or uploading the whole library for convenience;
- replacing professional asset-management metadata workflows in the near term.

These boundaries keep MemoLens focused: a private, inspectable bridge between a person's photos and what they are trying to remember or create.
