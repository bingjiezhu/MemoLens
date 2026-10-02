# Tasks: ML-015-B2A Canonical Blueprint Workspace & Honest History Bridge

- 状态：`IMPLEMENTED / VALIDATION CLOSURE WITH RESIDUALS`
- 完成标记规则：只有当前 checkout 的代码、测试、截图与证据都存在时才能把 `[ ]` 改为 `[x]`；不使用本 Spec 的预期作为完成证据。

## Phase 0 — Baseline and red oracles

- [x] **T001** 记录 branch、HEAD、package version、dirty worktree 和实际 writer runtime/SQLite version；保留用户及其他 agent 的已有改动。
- [ ] **T002** 冻结 V1–V5 migration tuple/checksum、managed V5 normalized `sqlite_schema` allowlist 与 fresh/v4→v5 schema snapshot。
- [x] **T003** 准备 healthy current、0/8/partial/8/8/post-edit authority、legacy brief/Timeline、corrupt head/ledger/lane 的合成 fixture；不使用私人媒体。
- [ ] **T004** 运行并记录 B0/B1/B1A、legacy Timeline、plugin safe-reader 与 renderer-model baseline；将环境阻断与代码失败分开。
- [ ] **T005** 先新增并观察 failing tests：healthy Blueprint project 的 409 断点、工作台无 canonical resume/compare/restore、无 honest executable/history projection。

Closure note：T002 要求的实施前 fresh/v4→v5 snapshot 未保留（当前 durable oracle 是 fresh/v3→v5）；T004/T005 要求的实施前 baseline 和 red-phase transcript/隔离 worktree 也未保留。当前 green regression 不倒签为历史证据，因此三项保持未勾选。

## Phase 1 — Contracts and models

- [x] **T006** 冻结 backend/Core workspace typed projection：root/current/exact `database_uuid`、project identity、canonical Blueprint、authority projection、`not_compiled`、3 lanes 和 capabilities。
- [x] **T007** 冻结 renderer TypeScript types/normalizers/reducer states，并禁止 Blueprint workspace 落入 legacy brief normalization path。
- [x] **T008** 冻结 12-section parent→result history diff 与 current→selected compare 的不同语义、顺序和 digest/value 处理。
- [x] **T009** 冻结 3-lane safe allowlist 与 latest bounded window/total/truncation 语义；authority lane 只允许 `event_id/sequence/authority_operation/observed_revision/decision_units/created_at`。
- [x] **T010** 新增纯模型测试：缺失/矛盾/未知字段、secret/path 注入、stale database/project/head、legacy fallback 和能力误升级全部拒绝或诚实降级。

## Phase 2 — Core/backend read bridge

- [x] **T011** 在一个 repository read boundary 中验证并投影 database/project、explicit head、exact current Blueprint、authority as-of revision、latest history windows 和 executable capabilities。
- [x] **T012** 实现 latest bounded `semantic_changes` projection，复用现有 operation/revision validation，返回 sequence/parent/result/changed sections、安全 digests、available totals 与 truncation。
- [x] **T013** 实现 latest bounded `decision_authority` 安全投影，不返回 nonce、token、receipt、runtime epoch/secret、path 或 raw presentation。
- [x] **T014** 实现 shared-budget bounded `legacy_artifacts` metadata 投影；lane/brief 固定 `migration_context_only`、Timeline 固定 `historical_observed_context_only`，使用精确 order literal，并在 canonical JSON decode 前执行 1 MiB/16 MiB byte preflight。
- [x] **T015** 将 healthy Blueprint project read 从 `creative_blueprint_workbench_unavailable` 409 替换为 canonical workspace 200；保持 exact legacy-only response 和 corrupt-head fail-closed 契约。
- [x] **T016** 实现 latest bounded windows：semantic/authority 从 available total 锚定并保持域内连续顺序；B2A 无 cursor/continuation，拒绝 renderer 任意 order/filter/SQL。
- [x] **T017** 保留 Blueprint project 的 legacy Timeline create/revise/new-render Core guard，并将 UI capability 从同一 server-derived 事实关闭。
- [x] **T018** 新增 API/Core focused tests：healthy/exact/corrupt/snapshot identity/privacy/boundedness/lane degradation/legacy regression。
- [x] **T019** 运行 no-migration oracle：V1–V5 的 5 条 migration 与 50 个 managed manifest object 通过 exact validator，fresh 与 v3→v5 的 87 个 normalized schema object exact-equal，new/missing object 均为 0；snapshot SHA-256 为 `0f265b1c8a868a1ef2944e17fdc3e0d23c844cf624ee1abe0756220c089858f6`。

## Phase 3 — Renderer state and API wiring

- [x] **T020** 扩展 project API client/normalizer，解析 canonical workspace 并保留 exact legacy-only project 兼容；任何 canonical marker 都触发 strict normalization，未知/marker-absent 形状不做 best-effort 真源推断。
- [x] **T021** 实现 canonical workspace state：loading/ready/reloading/conflict/integrity error、canonical head 与 selected historical revision 分开。
- [x] **T022** 修改启动/项目 resume：`localStorage` 仅用 project ID，必须 Core refetch；child/parent 双层校验 captured/current scope 与 database/project/head identity，stale response 不能覆盖新 state。
- [x] **T023** 连接 exact historical Blueprint read 和 12-section current→selected compare；不将 operation `changed_sections` 直接冒充 compare result。
- [x] **T024** 连接现有 desktop-authenticated restore command：Desktop HTTP 强制 `expected_database_uuid`，exact current/target digest，database/head/target 决定稳定 idempotency key，lost-response same-key replay，CAS conflict 不自动重试。
- [x] **T025** 成功 restore 后只通过 Core refetch 更新 current workspace；不在 renderer 手工拼接 revision/authority/history。
- [x] **T026** 新增 API normalizer/session/command tests：stale response、scope/database/project mismatch、conflict、replay、target corruption、capability false 和 legacy-only 兼容。

## Phase 4 — Workbench UX and visual QA

- [x] **T027** 实现 current Blueprint section workspace，提供可读摘要、展开细节和稳定 script block/evidence ID，默认不展示 raw JSON。
- [x] **T028** 实现 semantic proposal、decision authority 8 units 与 executable `not_compiled` 的独立状态和 honest language。
- [x] **T029** 实现 3-lane history UI，保留 lane/role/coverage 标签，不伪造 global total order 或给 authority/legacy row 提供不存在的 execute action。
- [x] **T030** 实现 compare/restore review，显示 current/target/database identity、changed sections、“创建新 revision”效果、取消与 conflict/reload；browser 只读检查与 Electron Desktop authenticated write 验收分开。
- [x] **T031** 用 compiler-unavailable honest empty state 替代 Blueprint project 的 legacy Storyboard/Timeline current canvas，Timeline/Preview/Render/Export CTA 与 Core capability 一致关闭。
- [ ] **T032** 完成 1440×1000、1280×800 与 390×844 响应式验收。已实查 1440×1000、390×844；另请求 1280×800 时浏览器实际只提供 1280×720，因此不冒充完整通过。
- [ ] **T033** 完成键盘、focus、ARIA live、不仅靠颜色和≥44×44 CSS px 触摸目标验收。当前仅验证 390px touch target ≥44px 与部分只读交互；完整 keyboard/focus 未完成。
- [ ] **T034** 保存真实 renderer/browser 截图并分开记录 browser read-only 与 Electron Desktop authenticated write：healthy、partial authority、compare/restore、CAS conflict、integrity failure、legacy lane unavailable、compiler unavailable、legacy-only regression。当前做过真实 browser 检查，但截图未归档为 repo-local artifact，writable flow 未跑。

## Phase 5 — Verification and evidence closure

- [x] **T035** 运行 latest-window adversarial oracle：删除/替换最新或中间 sequence 且 total 不变时，semantic/authority renderer 拒绝；同时验证 3 lanes 的域内次序与不完整/无全局序声明。结果包含在 latest renderer/session 18/18。
- [x] **T036** 运行 authority/legacy safe-projection privacy cases，secret/path/raw presentation/raw Timeline/locator 泄露为 0；结果包含在 focused API/model runs。
- [x] **T037** 运行 100-round Agent commit vs UI restore race 和 lost-response replay：Agent/UI winner 51/49、conflict 100、same-key replay 100、silent overwrite 0、final revision 101。
- [x] **T038** 运行 100+100+100 history fixture focused benchmark并记录环境/seed/raw samples/p50/p95。交易内 typed ledger attestation 后 25 次 warm read 的 p50/p95/max 为 `220.310 / 227.083 / 230.360 ms`，p95 较最初 `1760.719 ms` 降低 87.1%，500 ms 目标 `PASS`。fixture contract digest 和 25 个 raw samples 已记入 evidence。
- [x] **T039** 重跑 B0/B1/B1A Blueprint/authority/integrity 和 legacy Timeline guard 相关套件：106/106 passed；Python 3.14 `ResourceWarning` 仍保留为 residual。
- [x] **T040** 重跑 plugin safe-reader/MCP capability 套件：228/228 passed；MCP tools 仍是 `readOnlyHint=true`/`destructiveHint=false`，未增加 B2A network/media/file write capability。
- [x] **T041** 在当前 final diff 上运行完整 `npm run check`：Ruff、303 Core/backend Python、228 plugin、local verification、renderer/Electron build、87 Node 与 59 renderer-model 全部通过，exit 0。
- [x] **T042** 执行独立产品/架构/安全复审，覆盖真源、authority 误升级、history 过度声称、stale response、legacy fallback、schema drift 和响应式 UI；最终复审 `P0=0 / P1=0 / P2=0`。
- [x] **T043** 填写 [implementation-evidence.md](implementation-evidence.md)，仅引用实际文件、命令、结果、截图和审查结论。
- [ ] **T044** 只在所有 P0 gate 真实通过后将状态晋级为 `COMPLETE / LOCALLY VALIDATED`；B2A 不 bump version，并保留 Remote CI/tag/release 的真实边界。

## Explicitly deferred tasks

以下任务不得在本清单中顺手完成：

- [ ] `ML-015-B2B`：V6 migration、immutable Blueprint→Timeline binding、compiler/evidence manifest、stale compiled Timeline 检测。
- [ ] `ML-015-B2C`：跨 Blueprint/Coverage/Timeline/UI 的 unified operation history、undo/redo/branch/replay。
- [ ] `ML-018`：Script Coverage/Global Assignment 与真正全片素材分配。
- [ ] `ML-016/017`：Craft compiler、render/export、usage ledger 和轻量/完整素材包。

上述 deferred checkbox 在 B2A 完成时仍保持未勾选；它们是边界记录，不是 B2A 的失败。
