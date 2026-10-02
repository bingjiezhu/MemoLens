# Implementation Evidence: ML-015-B2B3

- 证据快照：2026-08-23，本地 shared dirty worktree
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCAL VALIDATION WITH RESIDUALS`
- 发布状态：`NOT COMMITTED / NOT TAGGED / NOT RELEASED`
- Remote CI：`NOT RUN`

## Checkout identity

| Item | Observed value |
| --- | --- |
| Workspace | `<repo-root>` |
| Branch | `codex/next-generation-creator-loop` |
| Base HEAD | `0caa2cf4afcb041150c131a41c4323a907dc6b6e` |
| Writer runtime | `.venv/bin/python` — Python 3.14.2 / SQLite 3.53.4 |
| B2B3 migration | V10 `canonical_timeline_revision_edit` |
| Current shared-tree schema | V11 `paired_agent_canonical_timeline_edit`（来自部分实现的 B2B4） |

## Observed implementation

- `core/timeline_edit_contract.py` 实现 closed `move_clip` / `trim_clip` / `set_clip_duration` / `replace_clip`，deterministic reflow 与 exact replay。
- `core/media_db.py` / `core/db.py` 在 B2B3 引入 V10 metadata-only migration，并将 exact edit 持久到 append-only Timeline operation/receipt；当前 shared tree 随 B2B4 部分实现已继续到 V11，V10 历史仍冻结不改写。
- `backend/src/media/timeline_lowering.py` 事务内重读 DB/project/Blueprint/Coverage/Timeline/source，解析 replacement source，以 head CAS 追加 N+1。
- `POST /v1/creative/projects/{project_id}/timeline/edit` 只接受 Desktop token、exact database/upstream/head preconditions 和一个 closed edit。
- `BlueprintProjectWorkspace.tsx` 提供可见的移动、裁剪、图片时长、same-Beat replacement、Discard 与 Save as revision N+1。未保存 preview 与 canonical resource 是不同类型，不能驱动 export/Usage。
- Save 成功后不采用 optimistic state；UI 必须重读 project 和 canonical Timeline，并对比完整 successor head。

## Focused verification

### Core / persistence / service / API

```text
PYTHONPATH=tests .venv/bin/python -W error -m unittest \
  test_timeline_edit_contract test_timeline_edit_persistence \
  test_timeline_edit_service test_timeline_edit_api \
  test_timeline_lowering_integration test_timeline_lowering_api -v
```

Observed：**50/50 passed in 10.786 s, exit 0**。

Timeline-wide discovery：

```text
.venv/bin/python -W error -m unittest discover -s tests -p 'test_timeline*.py' -v
```

Observed：**91/91 passed in 11.656 s, exit 0**。

### Renderer / build

- `npm run test:renderer-models`：**95/95 passed, exit 0**。
- `npx tsc --noEmit`：exit 0。
- `npm run build:renderer`：71 modules transformed，exit 0。
- `git diff --check`：exit 0。

Renderer 门禁包含：exact Coverage workspace admission、pending/canonical 类型隔离、完整 successor-head 比对、self-echo 不中断 reread、无歧义幂等键、stale-source/Coverage 优先级与可访问性边界。

### Existing Codex / DeepSeek plugin regression

B2B3 聚焦快照中的 `npm run test:plugin` 为 **252/252 passed in 43.620 s, exit 0**。随后 B2B4 增加 canonical editor/DeepSeek/receipt tests，最终整仓门禁后的独立复跑为 **267/267 passed in 48.874 s**。

### Final repository gate

`npm run check` 于 2026-08-23 在同一 shared worktree 新鲜完成，exit 0：

- Python unit discovery：**541/541 passed**。
- Plugin discovery：**267/267 passed**。
- Node/Electron：**108/108 passed**；Renderer models：**95/95 passed**。
- Ruff、local verification、typecheck、Renderer/Electron production build 均通过。
- 最终 `git diff --check` 另行通过。

默认 Python 3.14 aggregate run 会输出既有 SQLite connection finalizer `ResourceWarning`；门禁通过但不代表整仓 warning-clean。

## Fresh edit → reopen journey

`TimelineEditApiTests.test_edit_route_replays_receipt_and_exposes_current_mutation_capability` 使用 fresh 临时 SQLite 与真实 Flask route，执行：

```text
materialize revision 1
  → POST one image-duration edit
  → receive revision 2 result head
  → GET canonical Timeline again
  → verify selected revision 2 and edited 1400 ms duration
  → GET historical revision 1
  → verify old revision remains non-head and byte-semantically unchanged
  → GET project workspace
  → verify summary head revision 2 and timeline_mutation=true
```

该旅程是 non-mock DB/API 旅程，但不是人工点击的 fresh Codex Browser 旅程。Codex/DeepSeek 的可视 canonical handoff 属于 B2B4。

## Independent review

独立 UI/model 审查发现并已修正：

- `stale_source_binding` 与 Coverage stale evidence 同时存在时，Renderer 按 Backend 的 Blueprint → Coverage head → source → Coverage freshness 优先级接受合法投影。
- pending status live region 不再包含操作按钮；重复剪辑控件有 Beat/clip 语境。
- preview 只声明 content-addressed inspection，不声称 asset-id media route 已行使 exact fixed execution source。
- staging 公开模型现在要求完整 current Coverage workspace，并精确验证 database/head/content/evidence/operation binding。

## Residuals and non-claims

- 后续 B2B4/B2C0 已在 shared worktree 实现 Codex/DeepSeek canonical editor handoff、paired `timeline.apply_edit`、V12 generic receipt convergence 与 V13 Timeline history/restore；legacy editor 明确为 process-scoped Unsaved Draft Lab。fresh 双向真实 host/model/UI journey 仍未完成。
- 尚无人工 fresh Codex Browser 点击录屏/截图证据。
- final-fidelity preview、audio、subtitle、transition、split/delete、B2C unified history 与 Remote CI 未完成。
- Remote CI、commit/tag/release 均未运行；本地门禁结果不扩张为发布结论。
