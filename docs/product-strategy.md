# MemoLens product strategy

## Public product framing

MemoLens is a local-first desktop app: a private photo and video library, reversible Inbox review, Creator Memory, and a 720p first-cut preview. The GitHub homepage and README tell that desktop story. Running the app does not require a conversational plugin.

The 0.4–0.5 strategy also described an optional conversational plugin (Skill + MCP, read-only SQLite). That integration is not the public product promise. It remains documented below so the earlier architecture is not deleted: the desktop App is still the only confirmation and write surface.

## Product promise

MemoLens is the private media home for an independent creator: drop photos and videos into an approved folder, let the app remember and organize them, then describe the next post in the desktop App and work only from source-grounded material.

The product does not replace the file system and does not add another autonomous agent. Its advantage is the loop between **memory, lightweight review, creator intent, evidence, and reuse**:

```text
Drop media → understand it locally → review while remembering → describe an idea in the App → inspect why each asset fits → create → preview/save
```

## Primary users

1. **Independent short-video creator** — continuously captures material and wants to stop manually filing it before every post.
2. **Private archivist** — has years of personal photos and videos and wants recall without cloud migration.
3. **Hands-on editor** — wants an editable first cut, not an opaque one-shot generation.
4. **Conversational plugin user (0.4–0.5 strategy, optional)** — wants the same private memory available through conversation without another model key or agent runtime. This is not required for the desktop product.

## North-star outcome

> A creator can keep dropping in material, return with an idea days or years later, and reach a trustworthy, editable first cut without reorganizing the library first.

Supporting product measures:

- time to first indexed asset, first useful result, and first playable preview;
- percentage of new assets reviewed through a memory or creation flow rather than a cleanup chore;
- percentage of confirmed creator preferences that are used, overridden, and later kept;
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

### Remember before cleaning

Photos and videos are memories and creative material before they are storage usage. Review should happen inside a pleasant rediscovery or creation flow, with related moments visible together. Keep, archive, favorite, and project-ready are reversible MemoLens metadata; deletion is not a first-stage action.

### Personalization must be inspectable

Creator Memory only applies preferences the user pinned or confirmed. Every preference shows its source and revision. Search behavior, dwell time, raw prompts, and a model guess never silently become a durable preference. The user can override one project, reset the profile, or inspect the evidence without changing historical projects.

### One desktop surface, optional plugin reads

Earlier strategy treated the desktop App and a conversational plugin as two surfaces over one SQLite memory. The App still owns media state, permissions, confirmation, indexing, and rendering. Plugin code, if present, stays SQLite read-only and API-off by default.

Public docs now lead with the desktop App so the repository does not read as a second chat product. MemoLens does not ship a second chat system, a second memory database, or another agent scheduler.

## Product sequence

### Foundation — shipped in 0.2

- verified backend identity and loopback boundaries;
- collision-safe indexing and explicit empty/partial/failure states;
- mobile and split-view layout correctness;
- cancellation, real request state, basket hydration, and accessible recovery actions;
- repository CI, security policy, and an optional read-only MCP plugin that needs no separate model API key.

### Shipped — video creative workbench

- “Today in your memory” with forgotten highlights and recurring themes;
- editable intent chips and per-result explanations;
- adaptive video segmentation and timestamped mixed-media retrieval;
- drag-to-order storyboard, typed timeline revisions, local preview, and non-destructive Save As of the verified preview; final 1080p export remains gated on a scoped output grant;
- a safe-default MCP workflow for video search, draft creation, and offline validation;
- privacy/cost dashboard for local versus configured provider steps.

### Now — creator memory and media inbox

- a unified photo/video Inbox in Library with reversible Keep, Archive, Favorite, Ready, and Undo;
- related-moment review that favors remembering and creating over storage pressure;
- a versioned, evidence-linked creator profile that only learns after confirmation;
- creator preferences visible and overridable in Photo Story and Video First Cut;
- direct plugin context and Inbox reads through strict read-only SQLite;
- one App-owned confirmation surface for future scoped write capabilities.

### Later — memory intelligence

- incremental/background indexing with removable-drive recovery;
- opt-in people and relationship clusters entirely on-device;
- deterministic evaluation sets for retrieval relevance, diversity, grounding, and privacy;
- optional adapters for additional conversational surfaces while SQLite remains the source of truth.

## Non-goals

- silently reorganizing or deleting the user's media library;
- becoming a cloud sync or social publishing service;
- introducing a separate agent runtime, hidden profile learning, or an opaque autonomous organizer;
- hiding provider costs or uploading the whole library for convenience;
- claiming frame-perfect understanding, replacing a professional NLE, or executing arbitrary model-generated FFmpeg commands.

These boundaries keep MemoLens focused: a private, inspectable bridge between a person's media memory and what they are trying to create.

## Public walkthrough

The GitHub README opens with a 50-second English film of the same loop: remember locally, review Inbox, find a moment, then make a 720p first cut. It is a demo, not a substitute for the app: captions only, instrumental score, no voiceover, and no private library media.
