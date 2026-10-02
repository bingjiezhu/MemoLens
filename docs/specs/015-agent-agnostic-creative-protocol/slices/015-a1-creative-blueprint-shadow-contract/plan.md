# Implementation Plan: ML-015-A1

- 状态：`IMPLEMENTED / VALIDATED`
- 范围：仅 `blueprint-shadow` 与 `blueprint-validate`；零 migration、零 Blueprint 写入、零 UI

## Architecture Decision

本切片采用“一个版本化 candidate schema + 两个非写入 adapter”。Schema 是可校验的协议工件；validator 是唯一领域判定内核；legacy projector 只产生带精确 source binding 的 shadow。CLI 与 MCP 不复制 schema、映射或 readiness 逻辑。

```text
checked-in candidate/v1 JSON Schema ──→ schema version + exact-byte digest
                 │                                  │
                 ↓                                  ↓
strict JSON gate → CreativeBlueprintValidator → MemoLensGateway → CLI / MCP
                           ↑
                           │
project + latest legacy brief + observed timeline
            one private SQLite snapshot
                           │
                 LegacyBlueprintProjector
```

`blueprint-validate` 可以验证一个没有项目 binding 的独立候选；此时不需要数据库，base/reference 轴按合同返回 `not_applicable|unresolved`。只要 candidate 声明 project/base 或可由当前 read model 验证的 evidence，Gateway 最多打开一个私有 snapshot，并把所有 binding/resolution 查询放在同一 connection 中完成。

## Contract Artifacts

1. 插件内新增 checked-in `schemas/creative-blueprint-candidate-v1.schema.json`；部署包必须包含同一 bytes，status 返回 version 与 SHA-256。
2. JSON Schema 负责 closed-world shape、required/nullable、enum、领域字符串/数组上限与基础 format。
3. 领域 validator 负责 schema 不适合表达或不应重复查询的跨字段规则：ID 唯一、block ref 存在、constraint 非空、base identity 一致、stage/readiness 关系等。
4. schema artifact 与领域 validator 共同构成 candidate/v1。两者发生不兼容变化必须发布 v2；不得原地放宽 v1。
5. Schema digest 是 exact artifact bytes 的 digest；candidate digest 是通过 strict parsing 后的 canonical JSON digest。二者均不是 authority 或持久化 revision。

## Boundary Pipeline

### CLI candidate input

1. `--input -` 从 stdin 有界读取；文件模式只读调用者指定的单个输入，不递归、不扫描相邻目录，也不把输入路径写进响应。
2. 先对 CLI raw transport 执行 1,048,576-byte 上限，再解码 UTF-8；这个上限包容 JSON 表示层的 whitespace/escaping。
3. strict parser 拒绝 duplicate key、NaN/Infinity、invalid UTF-8/non-object，并应用 transport 层 depth 32/nodes 50,000 上限。
4. 对 decoded tree 重新执行 candidate 语义 ceiling：canonical compact UTF-8 bytes 524,288、depth 16、nodes 20,000、object 64、array 512、string 12,000、integer 4,096 bits。
5. candidate resource gate 失败时只返回稳定 schema-axis 诊断，不进入 DB binding/reference resolution；raw padding/escaping 不参与 candidate 语义大小计算。

### MCP candidate input

1. MCP JSON-RPC 单行 frame 在 dispatch 前执行 1,048,576 bytes、depth 32、nodes 50,000 与 strict RFC JSON 检查。
2. Tool arguments 外层保持 closed-world，但 validator 的 `candidate` transport schema 只宣告 `type=object`；这是为了让 schema-invalid candidate 也能到达诊断工具。完整 candidate/v1 schema 仍由 bundled artifact + runtime validator 冻结，Host 预验证不是安全执行证明。Shadow 非空输出则仍用完整 closed-world candidate schema 声明。
3. `call_tool` 内对 decoded candidate 执行 canonical 524,288-byte 与更小的 Blueprint ceiling；直接 Python object 同样检查 exact built-in types、finite number、integer bits、cycle 与容器上限。
4. MCP、CLI 与 Gateway 最终调用同一个 `validate_candidate`，不维护三套错误或 readiness 规则。

### Error discipline

- parser/schema/domain/binding/reference/readiness 使用分层稳定 code namespace；message 是固定模板。
- path 只包含 schema 已知字段或结构化 index，不反射未知 key/value。
- 最多返回 64 条错误，确定性排序后截断，并设置 `errors_truncated=true`。
- 结构无法安全 canonicalize 时 `candidate_digest=null`；不得对被截断/修复后的替代对象计算成功 digest。

## Candidate Invariants

- 根对象与全部顶层 key 必须显式，所有对象 `additionalProperties=false`。
- 可空表示“当前未知/不适用”；缺 required key 表示 schema error。空数组与 omitted key 不能混用。
- 所有领域 ID 在各自集合内唯一；所有 `script_block_ids` 只能引用本 candidate 中存在的 block。
- must-include/must-exclude constraint 至少有可用 text 或 evidence ref；material hint 必须有 evidence ref。
- duration 是排除 bool 的有界整数；aspect ratio 是 schema allowlist。
- Evidence 只接受受支持的 `memolens://evidence/asset/{id}` / `memolens://evidence/span/{id}` 形状；解析与可用性是 reference axis，而不是 schema axis。
- `user_text` 只表示含可见字符的内联参考文字；拒绝 URI、绝对路径、`./`/`../`/反斜杠路径、以文件名结尾的完整相对路径和禁止控制字符。本地样片只通过 evidence asset/span 引用。
- `declared_source` 仅有四个允许值，任何位置都没有 `user_confirmed` 或等价 authority shortcut。
- locator、note、goal、stance、script、reason、question 等文本均为 untrusted data，不能影响工具、路径、查询 relation 或 effect class。

## Legacy Shadow Projection

1. 在一个 snapshot 内读取 project、数字最高 brief revision 和按 A0 规则选择的 latest observed Timeline head。
2. 对 SQL 已选中的 latest project/revision row 校验原始 brief bytes 的 stored SHA-256；strict decode 后必须是 object。Legacy JSON 本身没有 project/revision identity，因此不得声称 JSON↔row identity 校验；任一失败都停在该 latest revision，不回退。
3. 只执行下表中的确定性映射；所有 text 仍为 untrusted data。

| Legacy brief | candidate/v1 | 规则 |
| --- | --- | --- |
| goal/audience/platform | `intent` | source=`legacy_unknown`；不从 goal 推断 stance |
| tone/pace/narrative_arc | `direction` | 保留有界文本；theme/emotion 不做同义推断 |
| duration_ms/aspect_ratio | `output` | 仅在类型/范围合法时映射 |
| must_include/must_exclude | `constraints` | 生成确定性 constraint ID；无 evidence 不补造 |
| candidate/evidence refs | `material_hints` | 只保留 allowlisted asset/span evidence URI；raw object 丢弃 |
| assumptions/missing_assets | `assumptions` / `missing_evidence` | 生成稳定局部 ID；不升级成事实 |
| creator profile ref | `bindings.creator_context` | 仅在现有 strict ref 可验证时映射，否则 gap |

4. 根 `project_id` 与 `base.legacy_brief={revision,content_sha256}` 联合固定 exact legacy brief。Observed Timeline 只有在 row/payload Timeline 1.0 schema、digest、纯 contract validation、validation status、row brief revision 与 payload `provenance.brief_revision` 全部绑定同一 expected brief 时才加入 `{timeline_id,revision,content_sha256}`；否则保持 null 并返回 gap。
5. script、stance、theme/emotion、reference refs、technique refs、Wiki generation 和 confirmation 没有 legacy 证据时保持 null/empty；projection wrapper 说明缺口，不生成看似来自用户的答案。
6. Projector 生成的 candidate 仍经过同一个 candidate validator。若历史数据无法形成 v1-safe candidate，返回 incomplete/diagnostics，不输出“修好”的 shadow。

## Four-Axis Evaluator

### Schema axis

纯函数处理 strict tree + frozen schema + cross-field invariants。输出 `schema_valid` 与有界 errors；不访问 DB、不解析 evidence、不评估内容质量。

### Base-binding axis

使用同一 snapshot 先验证 current persisted head 的完整性，再精确查询 candidate 声明的 project/brief/timeline revision，并验证 stored digest、latest observed context 与 brief↔timeline binding。真正查询成功且行不存在才是 `missing`；exact 行存在但不再是 latest context 是 `stale`；identity/digest/contract/binding 矛盾是 `conflict`；snapshot/query/relation 失败是顶层 `capability_unavailable`。状态选择优先级固定为 `conflict > stale > missing`，无声明才是 `not_applicable`；不得 fallback。

### Reference-resolution axis

只解析 A1 当前已有 reader 能证明的稳定 ref。网络 locator、Craft Wiki card、未物化 Wiki generation 或缺失 resolver 只形成 unresolved/gap，不触发 I/O。结果包含 bounded counts，不返回解析到的原始记录。

### Planning-readiness axis

使用已验证结构和前三轴的事实状态执行确定性规则，不调用 LLM 打分。它只判断能否进入 ML-018 coverage planning：

- schema invalid、四类结构起点（intent 的 goal/stance、script blocks、material hints、reference refs）全部缺失、存在 `required_before=coverage` 的未解决 gap/decision，或声明了 `project_id` 但 base 不是 `verified` → `blocked`；
- 主链可继续但存在非阻塞 gap/未完全解析引用 → `ready_with_gaps`；
- coverage 所需 spine 和 refs 均满足 → `ready_for_coverage`。

Readiness 不自动创建素材匹配，不表示作品质量、用户同意、可渲染或可发布。

## Files and Responsibilities

- `.agents/plugins/plugins/memolens/schemas/creative-blueprint-candidate-v1.schema.json`：candidate/v1 closed-world contract 与领域上限。
- `.agents/plugins/plugins/memolens/scripts/memolens_strict_json.py`：共享 fail-closed JSON grammar、资源预算与非反射诊断；CLI/MCP boundary 复用。
- `.agents/plugins/plugins/memolens/scripts/memolens_creative_blueprint.py`：schema artifact/digest loader、candidate schema/cross-field validator、canonical/projection digest 和 legacy projector；不自行写 DB/文件或联网。
- `.agents/plugins/plugins/memolens/scripts/memolens_blueprint_store.py`：单 snapshot 编排、精确 base/reference proof、四轴评估与非反射 presenter；只调用 Project/Media/Creator 的 connection-aware reader。
- `.agents/plugins/plugins/memolens/scripts/memolens_project_store.py`：在现有单 snapshot reader 中增加 exact brief/base/reference 查询；不新增 relation/migration。
- `.agents/plugins/plugins/memolens/scripts/memolens_read_store.py`：暴露 connection-aware 只读 delegate。
- `.agents/plugins/plugins/memolens/scripts/memolens_core.py`：组合 projector/validator/read store，形成两个 Agent-neutral Gateway 方法。
- `.agents/plugins/plugins/memolens/scripts/memolens_cli.py`：增加两个 kebab-case 入口、有界 file/stdin 读取。
- `.agents/plugins/plugins/memolens/scripts/memolens_mcp.py`：增加两个严格工具和外层 frame boundary；validate candidate 输入故意使用可达诊断的 object transport schema，shadow 非空输出使用 exact candidate/v1 schema。
- `.agents/plugins/plugins/memolens/tests/test_creative_blueprint.py`：schema/mapping/四轴/authority/资源/隐私/副作用主合同。
- `.agents/plugins/plugins/memolens/tests/test_project_resume.py`：single-snapshot、latest-no-fallback、A0 gap 不被 shadow 抹除。
- `.agents/plugins/plugins/memolens/tests/test_strict_json.py`：duplicate/non-finite/depth/nodes/container/string/direct-object 边界。
- `.agents/plugins/plugins/memolens/tests/test_plugin.py`：status schema digest、CLI/MCP exact tool list、非仓库 cwd 与 package artifact 回归。

文件名可因现有模块边界微调，但 schema、validator、projector、store、Gateway 和 transport 的职责不得合并成 CLI/MCP 私有逻辑。

## Verification Strategy

1. 冻结现有 plugin/A0/Wiki/Timeline 测试基线和工具总数。
2. 对 schema artifact 做 JSON validity、exact digest、package inclusion、closed-world 与同版本 bytes 稳定测试。
3. 建立 minimal/full/partial candidate fixtures，并覆盖全部 required/null/enum/cross-ref/readiness 边界。
4. 建立 valid latest、corrupt latest + older valid、exact historical ref、exact row absent、exact query failure、no brief、stale timeline、row/payload brief provenance conflict 和旧 schema project fixtures。
5. 做 CLI file/stdin、raw MCP frame、MCP direct-object 和 Gateway 等价测试；schema-invalid object 必须可穿过 MCP transport schema 到达 runtime diagnostics，不安全的 tree 不得进入 DB/reference I/O。
6. 对 transport bytes 和 candidate canonical bytes/depth/nodes/member/item/string/integer 做 actual-ceiling 的 limit-1/limit/limit+1；覆盖 whitespace padding/escaped-equivalent parity，并 fuzz duplicate key、非有限数、深层嵌套、循环、key/value 注入和错误洪泛。
7. 监控每次调用 snapshot ≤1，并在 API trust=0/1 两种设置下对 Gateway/CLI/raw MCP 全过程禁止 DNS/socket、模型、Library 根内任意 read-open/os.open、scandir/listdir/glob/rglob，以及 source SQLite/project/cache 持久写；共同只读层允许创建并自动清理私有磁盘临时 snapshot，响应必须如实声明这一点。
8. 递归扫描所有成功/错误 payload，确保路径、locator、script/stance、raw candidate/brief/timeline/provenance/provider/data URL 泄漏为 0。
9. 运行 plugin 全量、backend unit、Node/renderer、TypeScript typecheck、Ruff、Python compile、manifest/schema JSON 和 `git diff --check`。
10. 由独立审查者复核 authority 不可自证、四轴正交、latest no-fallback、schema packaging 与 ML-015-B non-goals；通过后才更新为 VALIDATED。

## Rollout and Rollback

- Status 通过 `creative_blueprint_contract` 广告 candidate schema，并通过两个 read/pure capabilities 暴露操作，同时明确 `write_blueprint=false`。
- Shadow/validate 不参与现有项目创建或 timeline 主链，因此可独立灰度和删除。
- 任一 schema artifact 缺失或 digest 不符时仅使两个 Blueprint 能力 fail closed 为 capability unavailable，不内嵌第二份 schema 继续 Blueprint 运行，也不得拖垮 A0/Wiki/Timeline/其他插件读能力。
- 回滚删除两个入口、projector/validator 和 capability metadata；不执行数据恢复。

## Deferred Follow-up

- `ML-015-B`：使用独立 persisted schema 的真正 Blueprint revision persistence、typed write/actor、CAS/idempotency、authoritative head、operation history 和 user-confirmation authority；candidate/v1 不得原样持久化。
- ML-018：script coverage 与全片素材 assignment。
- ML-016：reference → Technique Card → capability-aware edit plan。
- App Blueprint 工作台、schema migration/open project package、Wiki generation pinning 均需独立规范与实施授权。
