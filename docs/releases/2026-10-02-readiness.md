# MemoLens usability and publication readiness

Date: 2026-10-02. This source update consolidates the existing plugin-first implementation and fixes demonstrated first-use failures. It is a development preview, not a claim that every historical specification or a professional finished-film workflow is complete.

## Publication scope

The public `main` history and the local development history were rewritten separately. Their v0.5 source trees are identical, but their commit ancestry is not. The update is based on public `main` `3932f31`, preserves its license, community files, bilingual presentation and historical demonstration assets, and adds the current source/contracts/tests without force-pushing or publishing the private development history.

Private libraries, databases, credentials, runtime caches and private conversation transcripts are excluded. Historical design/evidence documents retain their dates and limitations; machine-specific usernames and private transcript links have been removed from the public copies. Earlier evidence is not a new test result.

## Usability fixes

| Failure | Correction |
| --- | --- |
| Successful cold Library bootstrap immediately quit the companion and interrupted scanning | Promote to normal startup only after Core commit, active proof and settings publication; cancellation/failure remain isolated |
| The selected hashed database was not discoverable after ordinary restart | Publish the verified backend settings projection and restore plugin managed-DB discovery |
| A fresh bootstrap database lacked its managed image-index baseline on restart | Initialize the required image schema before canonical migration, retaining strict post-migration verification |
| A plugin-created project could not be opened without renderer localStorage | Add an explicit project-ID entry, exact identity validation and stale-response protection |
| Missing or ambiguous managed databases silently selected an unrelated older Library | Keep an unavailable explicit/native selection unavailable and stop at ambiguous higher-priority state |
| Startup could silently restart an interrupted video and race its public recovery state | Preserve interrupted videos for explicit Resume; a deterministic event-barrier regression proves startup does not dispatch them |
| A prior project's late validation or operation response could update a newly opened project | Bind callbacks to project, Timeline revision, scope epoch and request generation |
| Renderer validators rejected the current Core image proof and clip fields, disabling native export | Validate the exact current image-analysis bindings in Coverage, Timeline and preview; add a production Python-to-TypeScript v1/v2 contract test |
| The DeepSeek npm package omitted required JSON schemas | Include and verify the real packed payload, not only the source checkout |
| Public and local code diverged on managed DB discovery and licensing | Preserve remote fixes and the existing license while retaining newer privacy/authority constraints |

The Browser editor retains the restored MemoLens light shell, muted sage accents, warm cards and dark media stage. Codex and DeepSeek use the same project state. The native companion remains responsible for folder selection, pairing and export.

## Validation record

Publication requires the exact candidate's GitHub CI checks to pass; see the repository Actions/PR checks for the commit-specific aggregate result. Local final-diff checks passed: 480 plugin tests, 197 Node tests, 148 renderer-model tests, 149 production-negative oracles, 37 Photon tests/build, real package closure (51 files, 3 schemas), typecheck/build, Python lint and dependency audits with no known vulnerabilities.

Remote macOS validation additionally exposed an unsafe SQLite library bundled with setup-python and an interrupted-video recovery race. CI now prepares and attests Homebrew Python's actual SQLite; the application safety policy was not relaxed. The video fix preserved the original recovery assertions and passed 23 macOS-lane tests plus 48 related focused recovery tests locally. DeepSeek `0.2.0-rc.2` installation and composed configuration also passed in an isolated Node 24.21.0 environment, without starting a Web/model session.

The first fresh-tree 1,113-test Core run exposed two public-branch integration mistakes: a retired legacy backfill was advertised again, and one Photon launcher bypassed the isolated Python runner. Both were corrected without weakening the boundary assertions; the complete 15-test setup module passed afterward. The strengthened creator journey and new production-to-renderer contract test also passed separately. These focused results do not replace the fresh CI aggregate gate.

Actual Electron UI verification used an isolated renderer profile and app state, with generated media and a prepared native-binding fixture. It opened an existing project without a saved session, loaded current Coverage/Timeline details, drove the real native export directory dialog and observed automatic polling reach **Export succeeded**. Independent ffprobe verified 1080×1920, 8 seconds and no audio. Four source hashes were unchanged; one export revision produced exactly two Usage records. A bad project ID preserved the current project. See [the path-free machine record](2026-10-02-native-ui.json). The operator was an automation agent, not a person or a fresh Codex/DeepSeek model journey.

The following checks are reproducible from the source:

```bash
npm ci
npm run check
npm run test:plugin-package
npm --prefix photon-bot ci
npm --prefix photon-bot test
npm --prefix photon-bot run build
npm run test:production-oracles
bash scripts/run_python.sh -m unittest discover -s tests -p test_release_creator_journey.py -v
```

The new creator service test uses two generated images in a private temporary folder. It commits one Library bootstrap, runs the real scanner and image-analysis worker with text-derived `semantic_hash`, updates that same project's Blueprint, materializes Coverage/Timeline, edits and replays one save, invokes real FFmpeg export and verifies the exact saved revision, output dimensions, silent audio policy, package files, usage and unchanged source hashes. It then closes and reopens the repository. This is not a native-user approval, fresh OS process or Agent/model test.

The separate bootstrap restart integration test launches real Python backend processes, commits a synthetic native binding, shuts down and starts normally twice, then checks plugin discovery, database UUID and scan recovery. Its approval is an automated fixture, not a person selecting a folder in a real dialog.

## Remaining limits

- Canonical source playback is muted; canonical export is a silent 1080p hard cut. Audio mixing, subtitles, transitions and final-fidelity playback are not delivered here.
- Metadata/semantic-hash fallback is text-derived, not visual understanding. Complete bounded Agent analysis exchange and materialized Wiki generations remain incomplete.
- Timeline revisions support explicit append-only restore, not a universal cross-resource undo/redo system.
- The lightweight export package is not a complete portable editable project with relinking.
- Automated approval fixtures, plugin loader checks and local aggregate tests do not establish real Codex/DeepSeek model acceptance, clean-machine native UX, signing or notarization.
- Existing Python 3.14 unclosed-SQLite `ResourceWarning` diagnostics are tracked resource-lifecycle debt, not silently suppressed.

See [the user guide](../user-guide.md) for supported actions and recovery, and [the requirement matrix](../audits/2026-09-04-convergence/requirements.md) for the broader product backlog.
