# UI refinement: less, but useful

Date: 2026-10-02. Baseline: `origin/main` at `6c9013498a3f88e56445d5eb67b5dc0e9ff8e2ff`.

Subsequent delivery status: the user authorized applying this version to the primary project, then explicitly requested a GitHub push after overall functional checks pass. See [publication verification](PUBLICATION.md). The local-only restrictions and test counts below describe the earlier review stage, not the current authorization or the later verification.

Follow-up: [local-only closeout review](CLOSEOUT.md), including the keyboard-order and resize/input-preservation fixes. The verification totals below describe the earlier pass; the closeout lists its own fresh results. Do not merge into the primary checkout or publish without a new explicit user request.

## Scope and design rule

The user asked for autonomous Grill Me self-questioning, simpler interaction, and a higher visual quality bar without replacing the existing MemoLens identity. Work took place in an isolated worktree; the original, dirty checkout was not changed.

The rule is **one primary action per task, progressive disclosure for uncommon actions, no empty controls**. This is an implementation and usability pass, not a claim of winning an award or completing unrelated product specifications.

The screenshots and browser interactions use a temporary, synthetic local library with two generated H.264/AAC color-card videos. No user library was accessed. Blue and green frames are intentional test media, not missing visual assets.

## Self-questioning and decisions

| Question | Evidence / answer | Implemented decision |
| --- | --- | --- |
| Does the first creation screen help someone start, or explain the product again? | The old page repeated a large title, introduction and local-privacy card before the input, especially on mobile. | Keep the page heading; move the workbench explanation and original-media guarantees into a collapsed guide below the workflow. |
| Must every future step look like an available button? | Five locked steps occupied space but could not be used. | Show the current `Step N of 6` status. Show step navigation only when earlier completed steps can actually be revisited; hide locked entries at every width. |
| Does opening an old project need a full form before a new idea? | It is a secondary path. | Collapse the existing-project form by default; reveal it for an active/opening project, errors or integrity failure. Preserve all existing checks. |
| What if someone collapses that form during a request which then fails? | A derived Boolean `open` alone did not observe the native toggle, hiding the new error. | Track native disclosure state, reopen on a new request/error or stable project identity change, and preserve deliberate collapsing during unchanged polling. |
| Should every clip have a full inspector visible? | This scaled page length with clip count and separated editing from the selected timeline clip. | Show only the selected clip inspector; automatically follow selection, seeking and version changes. |
| Can one mechanism replace several controls? | The timeline already supports clicking, scrubbing, edge handles and keyboard navigation. | Remove seven redundant buttons and the duplicate source-view position slider. Keep one Play/Pause button, one Zoom range and the timeline as the positioning surface. |
| Does an empty replacement entry help? | With zero eligible alternatives it offered no action. | Omit that disclosure; retain the reason in Source identity. When alternatives exist, keep the original current/candidate visual evidence and staging controls. |
| Does an opaque Beat ID identify a clip better than its position? | The inspector already retains source identity for verification. | Label timeline clips as Clip 1, Clip 2, etc.; keep opaque IDs in details rather than primary clip labels. |
| Does the remaining Play button cover replay? | Real browser testing found that playback at the end immediately finished again. | Play at the end now returns to the start before following the existing safe-preview policy. |
| Can simpler copy still distinguish an unsaved change? | Repeated technical terminology obscured the Save/Discard decision. | Use Unsaved change, Saved version, readable timecodes and Save version. Keep source-verification, audio and fidelity limitations in details. |
| Does rebuilding the interface strand keyboard focus? | Staging and saving replace the focused button's DOM node. | Recover focus to Save/Discard or the new selected clip; do not steal focus if the user moved to another input. Move focus into the next creation panel after its triggering control disappears. |
| Should mobile keyboard hints simply be hidden? | The same live region also reports trim, drag and pending state. | Keep that feedback region rather than hiding useful interaction results solely to save two lines. |

## Visual and interaction review

| Dimension | Applied / checked |
| --- | --- |
| Typography | Compact workspace titles; fewer competing large headings; timecodes and metadata remain subordinate to actions. |
| Whitespace | Desktop preview and selected inspector share a row; timeline spans the next row. Mobile timeline precedes the inspector. Creation input appears earlier. |
| Hierarchy | One selected inspector, one transport, one zoom control; only relevant step navigation and replacement choices. Unsaved Save/Discard remains explicit. |
| Color | Preserve MemoLens warm whites, deep green and muted sage. Do not copy a reference palette or add decorative gradients to editing controls. |
| Motion | Preserve reduced-motion support and existing restrained transitions. No decorative animation dependency added. |
| Microinteraction | Tested selection, keyboard seek, Play/Pause, end replay, disclosure toggles, staged edit/save/discard, history inspection and focus recovery. |
| Responsive | Actual browser widths 320, 390, 768 and 1280 were verified. Page scroll width equals viewport width for both tested surfaces. Horizontal scrolling remains confined to the intentionally zoomable timeline. |
| Originality | Retain the existing MemoLens brand and creation surface; borrow principles of restraint and editorial hierarchy, not reference assets or layouts. |

An independent screenshot review found no blocking visual issue in the tested surfaces. Its suggestion to reduce the technical Refresh label was applied. This is a scoped review, not WCAG certification, real-user research, or a promise that no future improvement is possible.

## Reference principles

- Media-first presentation with quiet controls: [Cosmos, Webby 2026 Best Visual Design — Aesthetic, People's Voice](https://winners.webbyawards.com/2026/apps-software-immersive/app-excellence/best-visual-design-aesthetic/380198/cosmos), viewed at [Cosmos Explore](https://www.cosmos.so/explore).
- Typography and negative space: [PP® Fragment, Awwwards](https://www.awwwards.com/sites/pp-r-fragment-1) and [FWA](https://thefwa.com/cases/ppr-fragment), viewed at [PP Fragment](https://pp-fragment.com/).
- These are design interpretations. No reference artwork, font files or screenshots are shipped in the application or this evidence folder.

## Real interaction evidence

- Two-clip saved version 1 was split using the actual editor. Save produced version 2 with three clips and the same 8,000 ms total duration, verified by rereading the Core projection.
- Removing a clip produced a two-clip, 6,900 ms unsaved preview; focus moved to Save. Discard restored the three-clip saved version 2 and focused its selected timeline clip.
- Historical version 1 remained read-only; viewing it did not replace the current saved version 2. Refresh returned to the current projection and source preview.
- Verified source playback stayed muted, progressed across clips and stopped at the end. Play at the end restarted at zero; the same button paused playback.
- At 320 px, expanding/collapsing Open existing project preserved its disabled library gate. Keyboard activation of Find material moved to Material and focused `video-step-materials-panel`. Only Idea and Material were then visible as usable navigation entries.
- These actions used the actual browser UI and synthetic Core fixture. They do not establish Codex/DeepSeek host acceptance, user approval for real mutations, provider availability, or final rendered-video fidelity.

## Automated verification

- TypeScript checks, including Electron types: passed.
- Production renderer build: passed.
- Renderer model and UI contract tests: 151 passed, including three added project-disclosure regressions. Focused project-open tests: 22 passed (included in the 151).
- Shared-editor real-JS behavioral tests: 33 passed, including zero/nonzero replacement candidates, end replay, pending/history preview gates and focus recovery.
- Canonical editor handoff: 38 passed. Safe playback: 11 passed.
- `git diff --check`: passed.

The previous merged release's complete Core and production-oracle suites were not rerun for this UI-only patch. Backend contracts, authorization, pairing, media verification, source preservation and export policy were not modified.

Project-disclosure regressions execute the real hook and component input expressions while simulating effect dependency scheduling and native toggle events. They are not complete browser-DOM tests of the request-failure race. The independent code review confirmed the original true-to-true disclosure failure is removed.

## Screenshots

Before captures are desktop baseline only. No unreliable viewport capture is labeled as a mobile baseline.

- [Before: creation, 1280 px](screenshots/before-create-desktop.jpg)
- [Before: shared editor, 1280 px](screenshots/before-editor-desktop.jpg)
- [After: creation, 1280 px](screenshots/after-create-desktop.jpg)
- [After: creation, 768 px](screenshots/after-create-tablet.jpg)
- [After: creation, 390 px](screenshots/after-create-mobile.jpg)
- [After: creation, 320 px](screenshots/after-create-320.jpg)
- [After: shared editor, 1280 px](screenshots/after-editor-desktop.jpg)
- [After: shared editor, 768 px](screenshots/after-editor-tablet.jpg)
- [After: shared editor, 390 px](screenshots/after-editor-mobile.jpg)
- [After: shared editor, 320 px](screenshots/after-editor-320.jpg)
- [After: unsaved change with explicit Save/Discard, 1280 px](screenshots/after-editor-pending.jpg)

Full-page mobile captures include the fixed bottom navigation at the original viewport boundary; it is not a second navigation bar in the page flow.

## Boundaries

This patch refines the shared plugin editor used by Codex/DeepSeek and the React creation entry/workflow. It does not redesign the separate `BlueprintProjectWorkspace` projection. The video-only QA fixture did not contain the Photo Atlas projection needed to open that React canonical project surface; its browser acceptance is not claimed here. The existing host/native mutation gates remain closed where authority is unavailable.
