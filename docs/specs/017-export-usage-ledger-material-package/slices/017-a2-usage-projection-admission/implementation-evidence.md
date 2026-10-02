# Implementation Evidence: ML-017-A2

- 证据快照：2026-08-23，本地 shared dirty worktree
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCAL VALIDATION WITH RESIDUALS`
- 发布状态：`NOT COMMITTED / NOT TAGGED / NOT RELEASED`
- Remote CI：`NOT RUN`

## Implemented truth surface

| Boundary | Current implementation | Honest limit |
| --- | --- | --- |
| Core absence/integrity | validated Usage reader cold-audits five Export relations before absence；double-delete orphan evidence fails closed | 若攻击者绕过外部 root 并完整删除某 project 在五张表的全部记录，本 SQLite slice 没有额外远端透明日志可证明其曾存在 |
| Projection | immutable occurrences → candidate-level used/residual；source bounds and consistent duration | 不物化 residual as new clip identity |
| Search/UI | unused/prefer/allow/used-in/residual filters；strict usage revision/11-field response；exact explanation | partial-used segment 可查看但 no-reuse 下不可选 |
| Brief authority | closed 9+11 selection；same `BEGIN IMMEDIATE` transaction revalidation；409 conflict/zero partial write | 未带 selection 的 legacy/all-mode create 保持旧自动检索语义 |
| Export recovery UX | exact submitted/unknown job bounded polling，terminal stop，manual Refresh retained | polling change after real Electron evidence; fresh interactive rerun pending |

## Focused verification

```text
MEMOLENS_PYTHON=/tmp/memolens-coverage-py314.WIkl3Q/bin/python \
PYTHONTRACEMALLOC=10 PYTHONWARNINGS=error::ResourceWarning \
bash scripts/run_python.sh -m unittest \
tests.test_canonical_export_persistence tests.test_canonical_export_artifacts \
tests.test_canonical_export_service tests.test_canonical_export_api \
tests.test_backend_process_lifecycle tests.test_backend_process_shutdown \
tests.test_usage_projection tests.test_usage_brief_admission \
tests.test_video_media.MediaRepositoryContractTests.test_analysis_commit_rejects_media_outside_probed_source_domain -q
```

Observed：**92/92 passed in 92.473 s, exit 0**。

- `npm run test:video-api`：12/12。
- `npm run test:renderer-models`：84/84（含 Timeline inspection model）。
- `npm run typecheck` 与 renderer build：passed。
- Director/API admission 专项：7/7；malformed 400、stale/conflict 409。
- `git diff --check` 与相关 Ruff：passed。

## Final repository gate

```text
git diff --check && \
MEMOLENS_PYTHON=/tmp/memolens-coverage-py314.WIkl3Q/bin/python npm run check
```

Observed：**exit 0**；Ruff passed；Core/unit **506/506 in 672.729 s**；plugin **231/231**；Node/Electron **106/106**；renderer models **84/84**；local verification、typecheck 与 Renderer/Electron build passed。完整默认 Python/plugin suites 仍显示既有连接生命周期 `ResourceWarning`，因此不声称 aggregate warning-clean；上面的 92-test focused lane 在 warning-as-error 下通过。

## Real Electron evidence already observed

真实 MemoLens Electron native directory flow 已生成：

`<TEMP_EVIDENCE_ROOT>`

- project：`proj_dd65c2a897cb4b4582d73606bb943ff0`
- observed job：`exportjob_442711adc0aa4a8eb0893f45744d4584`
- package：5 files；completion marker 与 manifest/hash 一致
- video：H.264、1080×1920、30 fps、8.000 s、无 audio stream
- revision 2 Usage：image `[0,4705)`；video Timeline `[4705,8000)` → source `[1000,4295)`

该交互运行使用 schema 8，并发生在 automatic polling/A2 admission 完成前；它证明 native export happy path 和物理 package，不证明 V9、A2 polling 或 Usage-aware brief UI 已在 fresh Electron session 验收。

## Residuals and non-claims

- exact residual selection identity、derivative-output exclusion、correction/supersession 未实现。
- fresh real Electron polling/admission、Remote CI、clean-machine、tag/release 未验证。
