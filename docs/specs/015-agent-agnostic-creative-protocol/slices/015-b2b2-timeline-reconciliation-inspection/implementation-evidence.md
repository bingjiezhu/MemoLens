# Implementation Evidence: ML-015-B2B2

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
| Writer runtime | `/tmp/memolens-coverage-py314.WIkl3Q/bin/python` — Python 3.14.2 / SQLite 3.53.4 |

## Observed implementation

- `core/media_db.py` 当前 schema 9；V9 扩展 Timeline command/receipt contract，保留 same-name public tables。
- `backend/src/media/timeline_lowering.py` 实现 `timeline.reconcile_from_coverage/v1`；API 是 Desktop-only closed POST。
- `src/blueprint/timeline*` 与 workspace 实现 exact reconciliation adoption 和 reason parity。
- `timelinePreviewModel.ts` / `CanonicalTimelinePreview.tsx` 实现无写入的 hard-cut inspection；`capabilities.preview=false` 保持不变。

## Focused verification

```text
MEMOLENS_PYTHON=/tmp/memolens-coverage-py314.WIkl3Q/bin/python \
PYTHONTRACEMALLOC=10 PYTHONWARNINGS=error::ResourceWarning \
bash scripts/run_python.sh -m unittest \
tests.test_coverage_contract tests.test_coverage_persistence \
tests.test_timeline_lowering_contract tests.test_timeline_lowering_service \
tests.test_timeline_lowering_integration tests.test_timeline_lowering_api \
tests.test_timeline_persistence -q
```

Observed：**106/106 passed in 134.366 s, exit 0**。

Renderer：preview model 5/5；最终 `npm run test:renderer-models` **84/84**；`npm run typecheck` 与 renderer build passed。

## Final repository gate

```text
git diff --check && \
MEMOLENS_PYTHON=/tmp/memolens-coverage-py314.WIkl3Q/bin/python npm run check
```

Observed：**exit 0**；Ruff passed；Core/unit **506/506 in 672.729 s**；plugin **231/231**；Node/Electron **106/106**；renderer models **84/84**；local verification、typecheck 与 Renderer/Electron build passed。完整默认 Python/plugin suites 仍显示既有连接生命周期 `ResourceWarning`，因此不声称 aggregate warning-clean；上面的 106-test focused lane 在 warning-as-error 下通过。

Populated migration artifact：`/tmp/memolens-v8-v9-populated.fBfGKE/populated-v8.db`。当前观察为 schema 9；Timeline operation/revision/head/receipt 各 1；Export operations/jobs/revisions/receipts 各 2，Usage 4；`PRAGMA foreign_key_check` 返回 0 行。两个 recoverable V8 backup/manifest 保留在同目录 `migration-backups/`。

## Residuals and non-claims

- V9 reconcile 与 inspection 尚未在 fresh real Electron session 中重新执行；此前真实 Electron export 使用的是 schema 8 数据库，不能替代本切片验收。
- inspection 是 muted hard-cut source check，不是 final-fidelity preview，不建立 approval/Usage。
- manual edit、manual-current reconciliation、B2C history、Remote CI 和 release 未完成。
