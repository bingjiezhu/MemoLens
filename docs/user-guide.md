# Using MemoLens

MemoLens is a local creative workspace with Codex and DeepSeek Harness adapters. This guide describes the supported development-preview workflow, including the native steps that the Browser editor cannot perform. 中文入口见 [README.zh-CN.md](../README.zh-CN.md)；界面按钮保留英文名称，便于按步骤定位。

## Install and keep the companion running

Follow [Quick start](../README.md#quick-start). Install Node.js and a supported Python interpreter first; the macOS instructions use Homebrew Python 3.14 and FFmpeg. The setup script creates the project virtual environment and installs application dependencies. Core refuses a Python/SQLite combination that fails its actual linked-runtime safety check. Changing only the Python version string does not bypass that check.

Install the chosen [agent adapter](../.agents/plugins/plugins/memolens/README.md#install-a-host-adapter). Start a new Codex task, or restart DeepSeek Web, after installing an updated plugin. Data reads do not need a new MemoLens model account. Provider-backed visual analysis is a separate optional capability, and metadata fallback must not be described as visual understanding.

The companion must stay running while scanning, pairing, editing or exporting. Closing the last macOS window can leave the app running; quitting the app shuts down workers safely. Reopen it to recover receipt-bound Library scans. Ordinary interrupted video jobs remain paused until you choose **Resume interrupted**; startup must not silently restart them. Do not expose port 5519 or the Browser editor port to the internet.

## Connect one Library

1. Ask the agent to check `memolens_status` and set up a Library if none is configured.
2. The plugin queues a path-free bootstrap request. When it says `queued_open_memolens`, open MemoLens yourself. This is not an automatic application launch.
3. Choose a folder in the native dialog. Cancellation does not create a project or grant access. Prefer `npm run demo:library` and the generated `demo-photo-library` when trying the app without private material.
4. After the exact Core commit and runtime verification, the companion stays open and the scan continues. The same confirmed Library is recovered on the next normal startup.
5. Ask the agent for `database.library_scan`. Discovery and analysis are different stages: imported files can still have queued child analysis jobs. An empty Library is a successful empty scan, not a ready Timeline.

Keep private media outside the repository. Do not move or replace the chosen root while scanning; a changed root identity requires explicit selection again, not automatic fallback to another folder.

## Open the same project

Ask the agent for the exact project ID. For diagnostics, the repository CLI provides:

```bash
python3 .agents/plugins/plugins/memolens/scripts/memolens_cli.py status
python3 .agents/plugins/plugins/memolens/scripts/memolens_cli.py project-list --status current
```

In **Create → Video first cut**, use **Open existing project** and paste that ID. This works without a previously saved browser session. A bad ID leaves the current project intact. A canonical project cannot silently fall back to its old brief or Timeline if verification fails.

The initial bootstrap Blueprint is an unverified starting proposal. Work with the agent to add a script and references to current asset/span evidence. The separate CLI pairing flow can grant bounded Blueprint commit/restore operations; MCP data tools themselves remain read-only. See the [plugin command guide](../.agents/plugins/plugins/memolens/README.md#developer-entry-points) for the exact commands. Pairing permission does not mean that you confirmed the creative decisions.

In the native project workspace, materialize the **Coverage Plan** and then **Materialize hard-cut draft**. Every Beat needs a currently resolvable assignment. If a gap remains, revise the proposal or choose appropriate evidence; scanning more files alone does not guarantee that the gap is resolved.

## Edit in the agent Browser

Pair only the actions you need in the native approval dialog:

| Action | Allows |
| --- | --- |
| `timeline.apply_edit` | Existing trim, duration, move and replacement controls |
| `timeline.apply_structural_edit` | Video split and removal of one Timeline occurrence |
| `timeline.restore_revision` | Restore a prior version as a new revision |
| `timeline.preview_media` | Bounded source playback with muted output |

No action implies another. Ask the agent to call `memolens_canonical_editor_handoff` with the project ID. Codex opens the exact returned Browser URL; DeepSeek presents **Open MemoLens Canonical Editor** for you to click. The handoff URL is session-specific: do not post it or treat it as a permanent project link.

Select a clip, position the playhead and use the visible controls. Changes remain **Pending** until **Save** succeeds and the saved state is reread. **Discard** writes nothing. Removing a clip removes an occurrence from the Timeline, not the original file. Restoring N1 while N2 is current creates N3; it does not overwrite N1 or N2.

**Unsaved Draft Lab** is a separate experiment surface. Its broader controls do not imply saved canonical features, and it cannot export a project.

## Export and check the result

Return to the same native project workspace and refresh its canonical head after Browser edits. Request canonical export, review the exact presentation, and choose a destination through the native picker. Existing output packages are never overwritten.

A successful package contains `video.mp4`, `script.txt`, `manifest.json`, `使用清单.txt` and a completion marker. It is currently a **silent 1080p hard-cut** output. The manifest and usage records refer to the exact saved Timeline and source occurrences. A queued job is not success; wait for `succeeded` and inspect the resulting file.

The older desktop 720p preview/Save As path is separate. Neither that path nor muted source playback proves finished-film audio, caption, transition or color-grading support.

## Recover without losing work

| Symptom | Next step |
| --- | --- |
| SQLite runtime rejected | Run setup again or select a runtime that passes `scripts/check_sqlite_runtime.py`; do not remove the safety gate. |
| Library scan stops after quit | Reopen the companion and query the same scan. Do not create a duplicate Library to restart it. |
| A video job is Interrupted | Use **Resume interrupted** in the Video first-cut job list. Resume creates the next attempt; a completed scan does not mean all its child analyses succeeded. |
| Plugin sees no current DB | Open the companion for the confirmed Library and restart the host. A missing binding or ambiguous managed DB must not silently select another database. |
| No Timeline / Coverage gap | Open the exact project, inspect its Blueprint and evidence, and materialize only a current gap-free plan. |
| Save blocked by expiry or restart | Re-pair the exact project/actions and request a fresh handoff. Previous saved revisions remain intact. |
| Head conflict after another edit | Refresh, review the current revision and restage the change. Do not automatically overwrite it. |
| Historical source cannot play | History inspection is read-only timing evidence. Return to the current revision, or explicitly stage and save a restore. |
| Export destination exists | Choose a new package name or folder. Do not delete an existing output merely to retry. |

## What this preview does not finish

Audio mixing, subtitles, transitions, comprehensive semantic media understanding, unified cross-object undo/redo, editable portable project packages and clean-machine signed distribution remain separate work. The [release record](releases/2026-10-02-readiness.md) distinguishes reproducible local checks from native user/model acceptance.
