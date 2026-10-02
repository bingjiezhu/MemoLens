# Implementation Evidence: ML-017-A Canonical 1080p Export & Success-only Usage Ledger

- 证据快照：2026-08-23，本地 shared dirty worktree
- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCAL VALIDATION WITH RESIDUALS`
- Focused gate：`PASS / 69 OF 69 / EXIT 0 / RESOURCEWARNING STRICT`
- 原始完整本地 gate：`PASS / CORE 471 / PLUGIN 231 / NODE 100 / RENDERER 71 / EXIT 0`
- 当前最终整仓 gate（含 B2B2/A2）：`PASS / CORE 506 / PLUGIN 231 / NODE 106 / RENDERER 84 / EXIT 0`
- 真实 Electron native directory/export/package：`RUN / SUCCESS OBSERVED`；A2 polling 后 fresh rerun 未执行
- Remote CI：`NOT RUN`
- 发布状态：`NOT COMMITTED / NOT TAGGED / NOT RELEASED`
- 证据规则：本文只记录 shared worktree 中实际观察到的实现和 residual；完整默认测试运行中的既有 `ResourceWarning` 不被写成 warning-clean，新 V8 focused lane 才有 strict warning-clean 证据。

更新说明：本文冻结 ML-017-A success-only writer/package 的原始 gate。后续 [ML-017-A2](../017-a2-usage-projection-admission/spec.md) 已实现 used/residual Search、strict Renderer adoption、same-transaction Brief admission 与 automatic bounded polling；A2 有独立 evidence，不把这些能力倒写成 ML-017-A 原始范围。

## 1. Checkout identity

| Item | Observed value |
| --- | --- |
| Workspace | `<repo-root>` |
| Branch | `codex/next-generation-creator-loop` |
| Base HEAD | `0caa2cf4afcb041150c131a41c4323a907dc6b6e` |
| Worktree | 有多个既有及并行中未提交变更；本切片未 commit/push/tag |
| Writer Python / SQLite | `/tmp/memolens-coverage-py314.WIkl3Q/bin/python` — Python 3.14.2 / SQLite 3.53.4 |
| FFmpeg / FFprobe | 8.0.1 / 8.0.1 |

Base HEAD 来自主实施线锁定的当前任务基线；最终门禁前必须重读 branch/HEAD/status，不应将本快照当作未来 checkout 身份。

## 2. Observed implementation truth surface

| Boundary | Observed current-worktree surface | Honest limit |
| --- | --- | --- |
| V8 schema | `core/media_db.py` 定义 independent canonical export operations/jobs/revisions/usage occurrences/receipts，以及 job-local immutable commit attestation、indexes/triggers/manifest/migration integration | fresh/upgrade/collision/tamper/rollback 已在 final-diff focused 与整仓门禁通过；不代表 hosted/clean-machine migration 已验证 |
| Native command | Electron main 固定 selection device/inode 并持有 descriptor；Core presentation/envelope/execute 固定 `main_native_user_gesture`、native nonce、prepared root descriptor、atomic root-locator/operation/job/receipt 和 idempotency replay | 已观察一次真实 native directory→success package；restart/unknown recovery 与 A2 polling 尚无 fresh interactive evidence |
| Success admission | Core 在物理发布前持久化 exact path-free commit attestation 并进入 `commit_pending`；完成/reconciliation 只接受与先验 attestation exact-equal 的 artifact/usage/runtime proof，再创建 Export Revision 和 Usage Occurrences | focused lane 已覆盖失败/取消/崩溃/替换包；真实断电与 clean-machine 故障仍未验证 |
| Canonical artifacts | `backend/src/media/canonical_export_artifacts.py` 定义 1080p intentional-silent hard-cut adapter、audio-stream absence probe、frozen source reads、runtime/actual-read proofs，以及 Core-held output-root `dir_fd` 上的 five-role package/inspection/quarantine | 保存的真实 FFmpeg 工件与 probe 已在第 5 节验收；字幕/封面及任何音轨能力仍未实现 |
| Service/outbox | `backend/src/media/canonical_export.py` 定义 presentation/start/read service、renderer-safe projection、single-worker job runner 与 interrupted reconciliation；durable Core success 保留包，deterministic rejection 才 quarantine，unknown/quarantine failure 保持 recoverable 并使 runner unhealthy | commit attestation → filesystem publication → final SQLite success 不是跨介质 ACID；activation recovery 未闭合时必须 fail closed |
| Runtime/API | `backend/src/__init__.py` 与 `backend/src/api/routes.py` 已出现 main-only presentation/start、safe job/exact successful revision reads、runner lifecycle/error mapping；revision 投影包含 path-free Usage Occurrences 和 digests | production 回归与一次真实 Desktop success 已观察；故障/unknown/restart UI 仍主要是自动化证据 |
| Electron authority | `electron/canonicalExportCoordinator.ts`、`main.ts`、`preload.cts` 使用 exact presentation → native directory selection + held device/inode → one-shot command，preload 仅暴露高层调用；post-dispatch 响应无法验证时返回 `unknown` | 不把 Node test 或源码 inspection 冒充真实 Electron 目录权限证据 |
| Renderer model/UI | `src/blueprint/exportTypes.ts`、`exportModel.ts`、workspace types/model/UI 定义 closed path-free contract、blocked/available/in-progress、explicit export，以及 `unknown` 后锁住再次批准；A2 增加 exact-job bounded polling 与 terminal adoption | success UI曾真实观察；polling/unknown/restart、mobile/keyboard/focus 尚无 fresh visual evidence |
| Legacy isolation | canonical export service/artifact path 不调用 legacy Timeline planner/`render_jobs`；新 V8 ledger 独立 | 整仓回归已通过；legacy successor 迁移、零 production caller 与 retirement 尚未完成 |

## 3. Closed v1 product boundary observed in code

- Profile：`export-1080p`，30 fps，`fit=cover`，有意 silent，hard cut；probe 必须证明不存在 audio stream。
- Geometry：`16:9=1920×1080`、`9:16=1080×1920`、`1:1=1080×1080`、`4:5=1080×1350`。
- Required package roles/files：`final_video/video.mp4`、`script/script.txt`、`package_manifest/manifest.json`、`human_usage_list/使用清单.txt`、`completion_marker/.memolens-complete.json`。
- Script：从 exact Timeline 绑定的 Blueprint script blocks 确定性投影，不成为第二份可写脚本。
- Optional roles：subtitle 和 cover 固定 `absent`，本切片不自动生成。
- Source policy：Library 原件不进入轻量包；导出前验证 exact source 并复制到 app-owned 临时 snapshot 供 renderer 读取。
- Package commit：Core 先持久化 immutable commit attestation；随后沿 held output-root `dir_fd` 在 staging 中 marker-last + fsync，目标目录 no-overwrite publication，再按同一 attestation 物理重验。只有 proven deterministic rejection 才进入 quarantine；unknown 或 quarantine failure 保持 recoverable/unhealthy。
- Usage：一 clip 一 immutable occurrence；video 保留 source half-open interval，image 不伪造 source time；used union/residual 不是可写表面。

## 4. Focused final-diff verification ledger

Exact command:

```text
MEMOLENS_PYTHON=/tmp/memolens-coverage-py314.WIkl3Q/bin/python \
PYTHONTRACEMALLOC=10 PYTHONWARNINGS=error::ResourceWarning \
bash ./scripts/run_python.sh -m unittest \
tests.test_canonical_export_persistence \
tests.test_canonical_export_artifacts \
tests.test_canonical_export_service \
tests.test_canonical_export_api \
tests.test_backend_process_lifecycle \
tests.test_backend_process_shutdown -v
```

Observed result: **69/69 passed in 90.560 s, exit 0**；同一运行把 `ResourceWarning` 提升为 error。

| Gate | Exact command / artifact | Current observation |
| --- | --- | --- |
| Core V8 persistence | 上述 strict command | 23/23 passed |
| Canonical artifact unit/fault suite | 上述 strict command | 21/21 passed；包含两个不同 video span 共享同一 actual-read source 的完整 package 回归，以及相同 source 指向不同 asset/digest 的反向拒绝 |
| Service/API/production wiring | 上述 strict command | service 17/17 + API 5/5 + lifecycle 2/2 + SIGTERM integration 1/1 passed |
| Native root identity / zero-locator rejection | 上述 strict command | selected inode swap、validate/reopen race、held-fd directory replacement、prepared-root atomic consume 均通过 |
| Durable attestation / replacement-package / unknown-unhealthy | 上述 strict command | attestation tamper/self-consistent replacement 拒绝；unknown/quarantine failure recoverable + runner unhealthy；durable success 后 transient read failure 可清除 |
| Electron coordinator | final `npm run check`；另以 `npm run test:node` 复核 exact counts | canonical coordinator 12/12；Node/Electron aggregate 100/100 passed |
| Renderer workspace/export models | final `npm run check`；另以 `npm run test:node` 复核 exact counts | renderer models 71/71 passed；workspace model 独立复核 25/25 |
| TypeScript/renderer/Electron build | final `npm run check`；`npm run typecheck` focused rerun | passed |
| Real FFmpeg five-role package | `<private-evidence>` | 保存并独立复核，详见第 5 节 |
| Fault/cancel/kill/reconcile/quarantine | 上述 strict command | 69-test lane 覆盖 pre/post-attestation crash、cancel、child reap、fsync/marker、publish race、quarantine/reconcile 与 graceful SIGTERM |
| V1–V7 + Blueprint/Coverage/Timeline/plugin/legacy regressions | final `npm run check` | Core/unit 471/471、plugin 231/231、Node/Electron 100/100、renderer models 71/71 passed |
| Final repository gate | `git diff --check && MEMOLENS_PYTHON=/tmp/memolens-coverage-py314.WIkl3Q/bin/python npm run check` | Ruff、local verification、typecheck/build 与全部上述测试通过，exit 0；未记录 aggregate wall-clock |
| Real Electron visual/native directory flow | `<TEMP_EVIDENCE_ROOT>` | native selection→job `exportjob_442711adc0aa4a8eb0893f45744d4584`→succeeded five-role package observed；该运行早于 A2 automatic polling |
| Remote CI / signed bundle / clean machine | run IDs/artifacts `TO BE RECORDED` | `NOT RUN` |

## 5. Preserved real-package evidence

保存根目录：

```text
<private-evidence>
```

该目录保留 tiny image/video 原始 fixture、V8 `media.db` 与最终五角色包。真实 image + bounded video span 经 FFmpeg 8.0.1 导出后，独立 `ffprobe` 观测为：**H.264、1080×1920、30/1 fps、8.000 s、22922 bytes、恰好一个 video stream、零 audio stream**。

| File | Size | SHA-256 |
| --- | ---: | --- |
| `video.mp4` | 22922 | `84d9c9782329f77e4ed47754d9807f3b7b954d3e2cfd609b020bd68d0f1b468d` |
| `script.txt` | 88 | `7e01faa6b6664d7e2fcf8815f11a8596ba7caada47947fd750bebe34a2d22b50` |
| `manifest.json` | 5619 | `6cd7e7fe93babee8fe02ad9ec109499b149d3baa62ae0d154b739e0ee7b5e98a` |
| `使用清单.txt` | 740 | `bd4d6a8f58e0de821dfc99fd3ec535df270767f2e69650b7d81408b130c43513` |
| `.memolens-complete.json` | 594 | `c508b7a20c83efccd14946921bd4d1a6759273f9eea4807c2514de6d08e89259` |

Core/manifest identities：job `exportjob_492e90e4bd8b48349d67423854b584b2`，operation/export `exportop_03b5fa2e7ab2487eb0e6576a3cbd96d4`，commit attestation `912493321c36f26c6d54713965818d37d15445d43ab5989d5a55899e02700c15`，Timeline revision 1，Export revision 1，Usage digest `ee640cd17d8c9b66be7cb50fca5866f09d5ddd063f7b253234050e76d1693b60`。只读 DB 复核计数为 operation/job/receipt/revision 各 **1**、Usage Occurrences **2**；job 为 `succeeded/completed/progress=1.0/attempt=1`。

原始 fixture digest 为 image `fe916f82daa6d3f0a6fffb67475a8851c012e87d406db790628bba91022b1e76`、video `3dfa1d63f5f7f951e4c7dffd17efe2edc4a65124e577108e7e197eea9dc8c4b9`，与 package usage manifest 的 source identity 完全一致。manifest 含 1 image + 1 video occurrence；video source 为 half-open `[1000,4295)`，Timeline 为 `[4705,8000)`，长度一致。另一次真实 Electron native flow 生成同内容语义的 revision 2 package，路径与 job 见上表；它证明 native happy path，但不替代 polling/unknown/restart 的专项交互。

后续完整产品验收仍需：

1. 真实 Electron unknown/restart recovery 与 A2 automatic polling；native directory + success 已观察；
2. 同一 native receipt replay 的真实 UI 零额外 mutation；
3. clean-machine、signed/notarized bundle 与 Remote CI 工件。

## 6. Side effects and recoverability boundary

已观察的设计将导出定义为 `native_user_egress`，只能写入 main 在当次原生交互中以 device/inode 固定并持有的 user-export root。Core 在 admission 前只准备 descriptor，不写 locator；命令成功后 worker/package/recovery 继续沿 Core-held `dir_fd` 操作。Library 原件只读；app-owned frozen snapshots 位于 job temp/cache 范围，不是用户包。成功包不是 cache，shutdown/rollback/cleanup 不得删除。

自动化故障注入已覆盖符号链接、root replacement、目标冲突、cancel/kill、fsync/rename 失败和已发布包的 quarantine/reconcile；真实 Electron native directory/success 已观察，unknown/restart、fresh A2 polling/admission、断电与 clean-machine 仍是 residual。

## 7. Residuals and non-claims

- 字幕、封面、转场、speed/reverse/freeze/nested sequence，以及任何音轨能力（source audio、配乐、旁白、混音）不在 ML-017-A；A 只承诺有意 silent 且 probe 无 audio stream。
- 未实现 ML-017-B 完整素材包；轻量包不包含原 image/video 或 used fragments。
- 未由本切片交付 Usage correction/supersession、final/test role、source relink 或 package repair UI。
- 本切片只建立 immutable occurrences；后续 A2 已交付 Search unused/prefer-unused/allow-reuse/residual projection 与 Brief admission，但 materialized Wiki、derivative-output exclusion 和 residual identity仍未交付。
- 不声称云发布、社交发布、自动上传、自动移动/删除/归档任何原素材。
- 不声称 legacy Timeline/render/export 已迁移或退役；ML-017-A 只建立不依赖它们的新路径。
- 真实 Electron native directory/success 已观察；A2 polling、unknown/restart、Remote CI、signed/notarized bundle、clean-machine 和 release仍是未验证 residual。
- 上游 V3 的可播放/可编辑完整初剪与真实产品黄金旅程尚未因为 V8 代码存在而自动成立。

## 8. Current conclusion

当前 shared worktree 已出现 ML-017-A 预期架构的主要代码表面：独立 V8 ledger、main-owned exact one-shot + held-directory approval、发布前 durable commit attestation、canonical 1080p intentional-silent hard-cut renderer、Core-held `dir_fd` 上的五角色 marker-last/no-overwrite 轻量包、unknown/unhealthy-aware `commit_pending` 恢复和 success-only Usage Occurrences。

ML-017-A 已记录 69/69 strict focused gate、完整本地 repository gate、保存的真实 FFmpeg package/ledger，以及一次真实 Electron native success package。A2 的 Search/admission/polling 见独立 evidence。unknown/restart/fresh polling、Remote CI、signed/clean-machine 和 release仍未完成，字幕/封面、任何音轨能力、residual identity、derivative exclusion 与完整素材包仍在范围外，因此不能表述为完整 V4 或可发布。
