# Implementation Plan: ML-015-B2B4C

- 实施状态：`IMPLEMENTED IN SHARED WORKTREE`
- 验证状态：`LOCALLY VALIDATED / PARENT B2B4 REAL-HOST JOURNEYS PENDING`
- 目标：在不扩大既有 `timeline.apply_edit` 权限的前提下，为 Codex/DeepSeek 共用 Canonical Editor 增加可持久化的 video Split 与 Timeline occurrence Remove。
- 主界面：Codex/DeepSeek 共用 `canonical-editor.html`；Electron 仅要求 reader/authority compatibility，编辑按钮不是首要交付。

> Phase 1–5 的主实现现已在 shared worktree 中存在，对应 focused contract/backend/export/migration/plugin UI/TypeScript 证据已观察。Phase 6 的 production inventory/oracle、final-version source discovery、fresh Codex reinstall/cache parity、official DeepSeek loader、独立审查、final-diff `npm run check` 与最终 evidence freeze 均已通过；父 B2B4 真实宿主旅程和 Remote CI/release 仍独立保留；详见 [Implementation Evidence](implementation-evidence.md)。

## Grill-me design decisions

### Question: 为什么不把两个 op 加入 `timeline.apply_edit`？

Recommended answer：既有 capability 已按四种低破坏 edit 获批；扩展其 closed dialect 会造成隐式权限升级。新建 `timeline.apply_structural_edit`，让用户重新看到并批准 durable structural mutation。

Why it matters：权限边界一旦错误扩张，后续日志完整也无法证明用户授权了删除。

### Question: 为什么不能继续写 schema v1？

Recommended answer：v1 冻结一 Beat/assignment/evidence 对一 clip；split/delete 的目标语义正是重复或缺失 occurrence。使用 schema v2 表达新基数，并要求 ledger replay 证明 v2 从合法父 revision 到达。

Why it matters：把不满足 v1 的状态伪装成 v1 会同时破坏 validator、restore、export 和 cold audit。

### Question: 最小可信 Split 是什么？

Recommended answer：只支持 video、绝对 `source_split_ms`、每侧至少 100ms、最多 256 clips、确定性 child IDs；不引入 image/audio split、bulk op 或 Draft Lab `offset_ms`。

Why it matters：这样可复用现有 video source-range proof 和 ID derivation，并把第一批失败面限制在可精确重放的范围。

### Question: 最容易被遗漏的下游失败是什么？

Recommended answer：export actual-read validator 当前拒绝 duplicate evidence，而合法 split 必然重复 evidence；React/Electron readers 也仍按 v1 唯一性解析。两者必须是 vertical slice 的组成部分，不是后续 polish。

Why it matters：否则会产生“Browser 保存成功，但 export/native reader 失败”的断裂 canonical head。

## Phase 0 — Freeze contracts and failing oracles

1. 冻结 `timeline.apply_structural_edit/v1` action、effect、request/response、paired/desktop routes、actor/origin 和 stable errors。
2. 冻结 `_STRUCTURAL_EDIT_FIELDS` 为且仅为 `split_clip`、`delete_clip`；先写额外字段、legacy dialect、caller result/source、bool/float 失败测试。
3. 冻结 Timeline schema v2 occurrence semantics及 v1/v2 version transition；先证明当前 v1 validator 对 split/delete fail closed。
4. 冻结 split interval/ID/binding、delete/ripple/source preservation 和 exact replay oracle。
5. 冻结 V18 normalized schema/checksum/future refusal；先写 populated V17 migration preservation oracle。
6. 冻结 export duplicate-evidence consistency rule、mixed v1/v2 restore 与 native reader compatibility oracle。
7. 只将已在实现后 diff 上观察的 focused/final lanes 改为 PASS；被后续修复超越的计数明示 superseded，任何未运行的 promotion gate 继续保持 pending。

## Phase 1 — Core structural contract and Timeline schema v2

1. 新建独立 `core/timeline_structural_edit_contract.py`，不要把 destructive op 混入普通 `_EDIT_FIELDS`。
2. 从 current parent Timeline 和 fixed source bindings 派生唯一 structural result；Browser/route 不传 result Timeline。
3. Split 只允许 video，精确生成左右 half-open source ranges、source bindings、Timeline positions 与 Core-derived stable child IDs。
4. Delete 只移除 target occurrence/binding，拒绝最后一个 visual clip，并确定性重排 ordinal/timing。
5. 扩展 `timeline_lowering_contract.py` 为 dual v1/v2 validator：v1 uniqueness 不放宽；v2 允许 proof-consistent occurrence 重复/缺失但 clip IDs 唯一。
6. 普通四-op edit 必须在 v2 head 上继续工作并保持 v2；v1 普通 edit 继续保持 v1。
7. cold audit 以 operation type 分派普通 edit、structural edit、restore replay；无合法父操作的 arbitrary v2 fail closed。

## Phase 2 — V18 migration and authority

1. 在 `core/media_db.py` 新增 V18，不修改 V1–V17 migration SQL/checksum。
2. 重建 Timeline revisions/operations、capabilities、generic receipts 和 capability events 的 closed CHECK/typed union。
3. 保存 V17 所有 row IDs、canonical JSON、digests、receipt bytes、event SHA/parent SHA、sequence 和 foreign keys。
4. 同步 Core/standalone plugin V18 name、manifest、checksum、normalized SQL、prefix commitments、resource budgets 与 future refusal。
5. capability action set 允许显式 structural action，但继续拒绝 wildcard、重复、未知、非规范排序和不批准的组合。
6. V18 copy/verify/swap 失败时事务回滚并保留受限 V17 backup；不做成功后的原地 V17 downgrade。
7. 将当前使用 schema 18 模拟 future-schema 的测试推进到 19，避免测试错误地把真实 V18 当成未来版本。

## Phase 3 — Service, API, CAS, receipts and restore

1. 在 `TimelineLoweringService` 增加 `apply_structural_edit` 与 paired variant，共用一个 Core mutation。
2. 增加 desktop/paired strict JSON endpoints；paired endpoint 只接受 exact structural action credential。
3. 在同一 verified transaction 中重读 head/upstream/source/capability，完成 operation/revision/head/receipt/use/event 与 exact successor admission。
4. 实现 same-request replay、different-request conflict、response-loss reconciliation、no-double-use 和 single CAS winner。
5. 对 operation/revision/receipt/use/head 每个阶段注入 fault，证明零部分写入。
6. 更新 B2C0 restore validator/service/history reader，精确支持 v1/v2 mixed chain 与 schema-preserving K→N+1。
7. 保持 `timeline.restore_revision` 独立权限；structural-only credential 不能 restore，restore-only/edit-only credential 不能 structural write。

## Phase 4 — Export, preview and all readers

1. 修改 export actual-read proof admission，允许同一 evidence 多次 occurrence，但 exact asset/source/proof identity 必须一致。
2. 每个 split child 产生独立 usage occurrence 与 half-open source interval；delete 后 target occurrence 必须从 export/usage 消失。
3. 不设置“一律禁止 source overlap”的错误约束；拒绝的是 proof/source substitution、错误 range 与 duplicate clip ID。
4. structural Stage 立即撤销旧 clip-scoped preview lease；Discard/Save+reread 后由 server 从 canonical head 重签。
5. 扩展 React/Electron Timeline types/models/API/readers，使其接受合法 schema v2 与 duplicate assignment/evidence occurrences。
6. Electron authority review/revoke 显示 structural action；Electron Split/Remove controls 可以 deferred。

## Phase 5 — Shared Canonical Editor and host docs

1. 在同一个 `canonical-editor.html` 增加选中 clip、playhead Split 与 Remove controls。
2. Split 仅在 video 且距两侧至少 100ms 时启用；server 对 Browser 计算出的 absolute source point 再验证。
3. Remove 在最后一个 clip、historical selection、无 structural authority 或已有互斥 pending 时禁用，并显示 source media 不变。
4. Split/Remove 只进入 `Pending · not canonical`；Discard 无写入，Save 后 exact canonical reread。
5. editor server 继续持有 credential/nonce/proof/idempotency identity；Browser 无 path/secret/actor/origin authority。
6. Codex plugin 与 DeepSeek Harness manifest/prompt/skill/README 公开同一 structural handoff 行为；不复制 host-specific contract/client。
7. Legacy tool/page/docs 保持 **Unsaved Draft Lab / Not saved / process-scoped**，并增加静态和运行时隔离测试。

## Phase 6 — Verification, evidence and promotion

1. 运行 pure contract、dual schema、V18 migration、service/API/CAS/idempotency/fault/cold-audit focused suites。
2. 运行 export/usage/preview/restore/native-reader/editor-server/security/UI/host-parity focused suites。
3. 运行 production surface oracle/inventory、plugin manifest validator、fresh reinstall/cache parity 与 source-cache diff。
4. 运行 warnings-as-error focused Python、full Python/plugin discovery、Node/renderer/typecheck/build、`npm run check` 和 `git diff --check`。
5. 独立复审 authority inflation、v1 history preservation、v2 replay proof、duplicate-evidence substitution、Draft Lab isolation 与 UI honesty。
6. 仅凭 exact observed commands/results 回填 `implementation-evidence.md` 和 tasks；未运行项继续未勾选。
7. B2B4 T053/T054 继续在原切片作为 fresh real-host blocker；本切片的 fixture、controlled Browser 或 adapter-process evidence 不改变其状态。

## Concrete implementation file map

### Core and schema

- `core/timeline_structural_edit_contract.py` — 新 structural parser/apply/replay contract。
- `core/timeline_edit_contract.py` — 普通四-op 对 v2 head 的兼容；不扩展其 action/dialect authority。
- `core/timeline_lowering_contract.py` — dual v1/v2 validation 与 occurrence invariants。
- `core/timeline_restore_contract.py` — schema-preserving mixed v1/v2 restore replay。
- `core/media_db.py` — V18 migration、typed unions、cold ledger dispatch。
- `core/production_surface_inventory.py`
- `core/production_surface_inventory.v1.json`

### Backend and export

- `backend/src/media/timeline_lowering.py`
- `backend/src/api/routes.py`
- `backend/src/agent_authority.py`
- `backend/src/media/canonical_export_artifacts.py`
- `backend/src/agent_preview.py`（仅在现有 lease API 不能满足 structural invalidation 时修改）

### Codex / DeepSeek plugin

- `.agents/plugins/plugins/memolens/scripts/memolens_canonical_editor.py`
- `.agents/plugins/plugins/memolens/scripts/memolens_editor_server.py`
- `.agents/plugins/plugins/memolens/scripts/memolens_agent_client.py`
- `.agents/plugins/plugins/memolens/scripts/memolens_agent_credentials.py`
- `.agents/plugins/plugins/memolens/scripts/memolens_agent_receipts.py`
- `.agents/plugins/plugins/memolens/ui/canonical-editor.html`
- `.agents/plugins/plugins/memolens/.codex-plugin/plugin.json`
- `.agents/plugins/plugins/memolens/prompt.js`
- `.agents/plugins/plugins/memolens/README.md`
- `.agents/plugins/plugins/memolens/skills/use-memolens/SKILL.md`
- `.agents/plugins/plugins/memolens/deepseek-harness/README.md`
- `.agents/plugins/plugins/memolens/deepseek-harness/skills/use-memolens/SKILL.md`

### Native/React compatibility

- `electron/agentAuthorityCoordinator.ts`
- `src/blueprint/timelineTypes.ts`
- `src/blueprint/timelineModel.ts`
- `src/blueprint/timelineEditModel.ts`
- `src/blueprint/timelineApi.ts`
- `src/blueprint/BlueprintProjectWorkspace.tsx`

### Tests requiring future-version adjustment

- `tests/test_image_read_cutover_migration.py`
- `tests/test_b2b4b_v15_preview_authority.py`
- `tests/test_b2c0_v13_timeline_restore.py`
- `.agents/plugins/plugins/memolens/tests/test_b1_agent_receipts.py`

以上测试中把 schema `18` 当作 future version 的 fixture 必须改为 `19`；只做机械版本推进并不足以验证 V18 migration。

### Documentation updated with the implementation

- `README.md`
- `docs/specs/roadmap.md`
- B2B4/B2C0 successor references（不改写其历史实现范围或已记录证据）
- B2B4 `editor-donor-adoption-2026-08-29.md`（只记录交互概念借鉴，不声称复制 legacy implementation）

当前文档收口同步更新根 README、规格索引/roadmap、Codex plugin 说明、DeepSeek Harness 说明与两份 Skill；不改写旧切片的历史证据。

## Rollback and reversibility

- 功能回滚时停止签发 `timeline.apply_structural_edit` 并隐藏 Stage/Save controls；已写入的 v2 revisions 必须继续被 reader、restore、export 和 cold audit 支持，不能降级改写。
- V18 migration commit 前失败必须完整回滚并保留 verified V17 backup；成功后不做 destructive downgrade。
- 用户内容回滚使用 append-only `timeline.restore_revision`，不删除 structural operation/revision/receipt/event。
- 如果 export/native reader 还不能消费 v2，则 structural write 必须保持关闭，不能先保存再补兼容。
- 无法证明 source/proof/authority/CAS/receipt 连续性时 fail closed，不回退到 Draft Lab save 或 caller-supplied Timeline。
