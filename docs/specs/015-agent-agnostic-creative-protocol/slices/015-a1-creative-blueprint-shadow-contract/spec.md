# Feature Specification: Creative Blueprint Shadow Contract

- Feature ID：`ML-015-A1`
- 创建日期：2026-08-22
- 状态：`IMPLEMENTED / VALIDATED`
- 实施授权：用户已授权将 Grill Me 共识按小切片实现；本切片只授权 Creative Blueprint shadow 与 candidate validation 两个非写入能力
- 父规范：[ML-015 Agent-Agnostic Creative Protocol](../../spec.md)
- 前置切片：[ML-015-A0 Cross-Agent Project Resume](../015-a0-cross-agent-project-resume/spec.md)
- 产品决策：[43 问产品对齐记录](../../../product-decisions-43-questions-2026-08-22.md)
- 优先级：P0，先冻结可验证的创作语义，再开放任何 Agent 写入

## Overview

ML-015-A0 让外部 Agent 能从 legacy creative brief 和最新观测 Timeline 恢复项目，但它刻意保留了 `creative_blueprint_unavailable`：现有 brief 没有可靠表达立场来源、脚本块、参考、技巧、素材约束、未决问题及其 authority，不能直接重命名为 Creative Blueprint。

本切片建立两个相互补充、但都没有副作用的能力：

```text
已验证的 latest legacy brief
  → blueprint-shadow <project_id>
  → 明确非权威、未持久化的 candidate/v1 投影

Agent / 用户工具产生的 candidate JSON
  → blueprint-validate --input <file|->
  → schema / base binding / reference resolution / planning readiness 四轴结果
```

Creative Blueprint candidate/v1 是 Agent 中立的协商/预检 envelope，不是当前项目 revision，也不得被 ML-015-B 原样持久化成真源。Shadow 只是把已存在的 legacy 字段投影到该 envelope，帮助 Codex、Claude 或其他 Agent 在同一语义结构上继续讨论；validator 只判断候选“结构是否成立、绑定是否可验证、引用能否解析、是否足以进入素材 coverage”，不批准用户立场，不持久化候选，也不生成 Timeline。后续真源必须使用独立的 persisted schema、typed operation actor、precondition 和 authority ledger。

## User Scenarios & Testing

### User Story 1：旧项目能进入统一蓝图语义，但不会被伪装成新真源（Priority: P1）

**Given** 一个包含完整、digest 正确的 latest legacy brief 的项目，**When** Agent 调用 `blueprint-shadow`，**Then** 它得到 `memolens.creative_blueprint_shadow`，其中可映射字段进入 `memolens.creative_blueprint_candidate`，无法证明的立场、脚本、参考和技巧保持空缺，并明确 `is_authoritative=false`、`is_persisted=false`、`authority_verified=false`。

**Independent Test**：只准备 project + legacy brief fixture，不创建 Blueprint 表或新 revision；验证字段映射、source binding、candidate digest、缺口和 source/persistent state 零写入。

### User Story 2：任意 Agent 在请求写入前都能用同一合同预检候选（Priority: P1）

**Given** Codex 与 Claude 分别产生 candidate/v1 JSON，**When** 它们通过 CLI 或 MCP 调用 `blueprint-validate`，**Then** 两个入口经同一 Core validator 返回相同 schema digest、candidate digest、四轴结果、稳定错误码和有界摘要，不回显完整候选。

**Independent Test**：对同一 canonical candidate 同时调用 Gateway、CLI 和 MCP，逐字段比较领域结果；不需要模型、网络、App UI 或写权限。

### User Story 3：结构正确不会被误报为“用户已确认”或“可直接剪辑”（Priority: P1）

**Given** candidate 结构完全有效，但立场仅由 Agent 提议、base 已过期或素材引用未解析，**When** validator 运行，**Then** `schema_valid=true`，其余轴分别返回 `stale|partial|blocked` 等真实状态；`caller_declared_user_input` 仍只是调用者声明，不能使 `authority_verified` 变为 true。

**Independent Test**：以同一 candidate 分别制造 authority 自证、stale base、部分 evidence 和 blocking open decision，确认四轴互不吞并且 planning readiness 不被 schema validity 替代。

### User Story 4：恶意或超大 JSON 在进入领域逻辑前失败（Priority: P1）

**Given** 输入包含重复 key、NaN/Infinity、无效 UTF-8、C0/DEL 控制字符、超深结构、过多节点、过长字符串、绝对/完整相对文件路径或把素材文本伪装成控制指令，**When** CLI/MCP 接收该输入，**Then** 系统 fail closed，返回不反射敏感值的稳定诊断，并且不打开数据库以外的文件、不联网、不读取原媒体、不修改任何状态。

**Independent Test**：对每个资源边界做 limit-1/limit/limit+1 测试，同时监控 snapshot 次数、DNS/socket、媒体文件 open 和 DB/WAL/SHM bytes。

## Candidate/v1 Contract

### Identity and Required Shape

仓库必须包含一个受版本控制的 JSON Schema 工件。其精确 bytes 的 SHA-256 是 contract digest；现有 status 能力响应必须公开 candidate object、version 和 digest，使 Agent 在提交前确认自己使用的合同。A1 不增加第三个 schema 工具。

Candidate 根对象固定为：

- `object = "memolens.creative_blueprint_candidate"`
- `schema_version = "1"`
- 下列顶层字段全部显式出现，且所有对象均为 closed-world；未知字段不得被静默保留或忽略。

| 顶层字段 | v1 语义 |
| --- | --- |
| `project_id` | 可空的不透明项目 ID；存在时与 `base` 的 exact brief/Timeline refs 共同固定同一项目上下文 |
| `base` | 可空；只允许 exact legacy brief ref 与 latest observed Timeline ref，不表达 authoritative project head |
| `intent` | `goal`、`stance`、`audience`、`platform` 与 `declared_source` |
| `script` | `declared_source` 与有序 `blocks[{block_id,text}]`；空数组表达尚无脚本，不以缺 key 表达 |
| `direction` | `theme`、`narrative_arc`、`emotion`、`tone`、`pace` 与 `declared_source` |
| `output` | `duration_target_ms` 与 `aspect_ratio`；只表达创作目标，不触发渲染 |
| `constraints` | `must_include` / `must_exclude`；每项为 `{constraint_id,text,evidence_ref,script_block_ids}`，其中 text/evidence ref 可空但不能同时无意义 |
| `material_hints` | `{hint_id,evidence_ref,script_block_ids,reason}`；reason 可空，hint 不等于已选 Timeline clip |
| `reference_refs` | `{reference_id,kind,locator,note}`；note 可空，locator 永远按不可信数据处理且 A1 不自行访问网络或文件；`user_text` 只表示内联参考文字 |
| `technique_refs` | `{card_id,revision,declared_state}`；只引用技巧候选/选择声明，不证明技巧可执行 |
| `bindings` | 可空的 `creator_context` 与 `wiki_generation` binding；不得用 current global state 替代声明的 pin |
| `assumptions` | `{assumption_id,text}`；与已验证事实分开 |
| `missing_evidence` | `{gap_id,description,script_block_ids,required_before}` |
| `open_decisions` | `{decision_id,question,scope,required_before}`；用户尚未作出的决定不能被 Agent 补成答案 |

所有 ID、enum、nullable、字符串和数组的领域上限由 candidate/v1 schema 工件冻结；领域上限不得大于通用 JSON ceiling，并有边界测试。修改字段、enum、required/nullable 语义或提高资源上限都必须发布新 schema version，不能只替换同版本工件。

v1 已冻结的关键领域边界为：

- opaque ID 使用 `^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$`；revision 为 1–1,000,000，content digest 为 64 位小写十六进制 SHA-256。
- script blocks 与 material hints 各最多 256；每个 `script_block_ids` 最多 128 且不允许重复。
- must-include、must-exclude、reference refs、technique refs 各最多 64；assumptions、missing evidence、open decisions 各最多 100。
- reference `kind` 只接受 `project | research_snapshot | evidence | external_https | user_text`。`user_text` 必须是含可见字符的内联文字，不得是 URI、绝对路径、`./`/`../`/反斜杠路径或以文件名结尾的完整相对路径。本地样片必须先进入素材库并以 evidence asset/span 稳定 ID 引用。分类/节奏文字中的普通斜杠不视为路径。
- technique `declared_state` 只接受 `proposed | selected | rejected`；它是选择声明，不是已验证 user confirmation。
- `required_before` 只接受 `coverage | timeline | render | publish | not_blocking`；open-decision `scope` 只接受 `intent | stance | script | direction | output | material | reference | technique | other`。
- `duration_target_ms` 为排除 boolean 的 1,000–1,800,000 整数；`aspect_ratio` 只接受 `16:9 | 9:16 | 1:1 | 4:5 | null`。
- 非空文本必须至少包含一个可见 Unicode 字符；NUL 等 C0（明确允许的 tab/newline/carriage return 除外）与 DEL 控制字符拒绝。被允许的空白/换行不能单独满足“可见文本”。

### Authority Is Not Self-Proving

- `declared_source` 只接受 `caller_declared_user_input | agent_proposal | legacy_unknown | unknown`。
- v1 不接受也不生成 `user_confirmed`；调用者不能通过字段名、自由文本、MCP 参数或 provenance 自证用户已确认。
- `caller_declared_user_input` 的含义仅是“调用者声称这来自用户输入”，不是 MemoLens 已验证 authority。
- Shadow 的映射 section 使用 `legacy_unknown`；缺失值保持 null/empty，不得根据 goal、title、candidate 或 provenance 猜测立场。
- 两个工具的 authority 结果始终包含 `authority_verified=false`。真正的 user confirmation 需要后续带 actor/precondition 的 operation contract，不属于 A1。

### Digest Semantics

- `candidate_digest` 是通过 strict JSON resource gate 后的完整 candidate canonical JSON SHA-256；它只用于识别语义等价的本次提案。
- `material_constraint_projection_digest_v1` 只对以下精确投影计算 canonical SHA-256：完整 `constraints.must_include`、完整 `constraints.must_exclude`，以及每个 material hint 的 `{evidence_ref,script_block_ids}`。`hint_id` 与 `reason` 刻意不在该投影内。
- 上述投影 digest 只能用于比较“素材硬约束/显式候选是否变化”；它不是 coverage cache key、检索计划 identity、签名、authority proof 或 persisted revision digest。未来 coverage 必须把 script、intent、reference、Wiki generation 和 planner version 等自己需要的输入纳入独立合同。

### Four Independent Validation Axes

Validator 必须同时返回四个轴；不得把单一 `valid`、综合分数或 LLM 评价替代它们。

| 轴 | 固定结果 | 判定边界 |
| --- | --- | --- |
| `schema_valid` | boolean | 仅表示 strict JSON + candidate/v1 closed-world schema 与领域不变量通过；不代表事实、authority 或可剪辑性 |
| `base_binding.status` | `verified \| missing \| stale \| conflict \| not_applicable` | exact project/brief/timeline identity、revision、digest 与 binding 在同一 snapshot 内的结果 |
| `reference_resolution.status` | `verified \| partial \| unresolved \| not_applicable` | A1 当前受支持的 evidence/binding ref 能否按稳定 ID 解引用；不访问 locator、网络或媒体来“补齐” |
| `planning_readiness.status` | `ready_for_coverage \| ready_with_gaps \| blocked` | 是否已有足够 intent/script spine 进入后续素材 coverage，以及 required-before gaps/decisions 是否阻塞 |

判定规则至少满足：

1. schema 失败时 planning readiness 必须为 `blocked`；其他轴只能基于仍可安全读取的 identity 给出结果，不能尝试修复输入。
2. `project_id=null` 且 `base=null` 时 base binding 为 `not_applicable`；只要调用者声明了持久化 base，就必须验证，不能因找不到而降级成 not applicable。
3. exact project/brief/timeline ref 查询成功且行不存在为 `missing`；exact ref 存在、digest 相符，但已不是声明的 observed/latest context 为 `stale`；digest、project identity、persisted contract 或 brief↔timeline binding 互相矛盾为 `conflict`；snapshot/query/relation 能力故障必须使顶层结果为 `capability_unavailable`，不得伪报为“行不存在”。任何失败都不回退旧 revision，状态优先级固定为 `conflict > stale > missing`。
4. 没有需要解析的 ref 时 reference resolution 为 `not_applicable`；全部支持的 ref 解析成功为 `verified`；部分成功为 `partial`；一个也无法证明或 resolver capability 缺失为 `unresolved`。
5. `ready_for_coverage` 只说明后续 coverage planner 可以开始，不说明用户确认、素材充足、技巧可执行或 Timeline 可渲染；存在可继续处理的非阻塞缺口时为 `ready_with_gaps`。四类结构起点——intent（goal 或 stance）、script blocks、material hints、reference refs——全部缺失，存在 `required_before=coverage` 的 gap/decision，schema 失败，或 candidate 声明了 `project_id` 而 base 不是 `verified`（`missing|stale|conflict`）时均为 `blocked`。未绑定项目（`project_id=null,base=null`）可在其他条件满足时继续。
6. schema errors 返回受限 `{code,path,message}`；reference issues 返回 `{code,path}`；base issues 与 planning blockers/gaps 返回稳定 code 数组。所有 message 使用不反射输入值的固定模板。Schema errors 最多 64 条并返回 `errors_truncated`；reference issues 最多 64 条并返回 `issues_truncated`，但 total/verified/unresolved 计数和 planning blocker 必须基于完整有界集合，不得被公开诊断截断影响。

## Functional Requirements

- **FR-A1-001**：CLI 和 MCP 必须从同一 `MemoLensGateway` 暴露且只新增以下两个领域能力：CLI `blueprint-shadow <project_id>` / `blueprint-validate --input <file|->`；MCP `memolens_blueprint_shadow` / `memolens_blueprint_validate`。
- **FR-A1-002**：Shadow 成功对象固定为 `memolens.creative_blueprint_shadow`，validate 成功对象固定为 `memolens.creative_blueprint_validation`；candidate 固定为 `memolens.creative_blueprint_candidate` / schema version `1`。
- **FR-A1-003**：candidate/v1 schema 必须是随插件发布的版本化 JSON 工件；现有 status 必须返回其 version 与精确 bytes SHA-256。不得增加 schema dump/tool，也不得从网络下载 schema。
- **FR-A1-004**：CLI 文件/stdin 是单个 transport envelope，原始 UTF-8 JSON 最大 1,048,576 bytes，并在 dispatch 前使用 strict RFC JSON 解码。解码后/direct in-memory candidate 的 canonical compact UTF-8 JSON 最大 524,288 bytes、depth 16、nodes 20,000、单 object 64 members、单 array 512 items、单 string 12,000 chars、整数最大 4,096 bits。超出 candidate ceiling 必须在任何 DB binding/reference resolution 前 fail closed，但 raw whitespace 或等价 JSON escaping 不得改变领域判定。
- **FR-A1-005**：MCP 外层单行 frame 最大 1,048,576 bytes、depth 32、nodes 50,000；frame 解码后的 candidate 应用 FR-A1-004 的 canonical 524,288-byte 与更小结构 ceiling。直接传入的 in-memory object 也必须检查 exact built-in JSON types、cycle、finite number、整数 bits、深度、节点和容器大小。MCP arguments 外层仍为 closed-world；`candidate` 的 advertised transport schema 必须允许任意 JSON object，使 schema-invalid candidate 也能达到 validator 并获得诊断，不能假设 Host 已执行完整 candidate/v1 schema。
- **FR-A1-006**：两个入口必须拒绝 duplicate key、NaN/Infinity、invalid UTF-8、non-object root、bool-as-integer、过大整数、未知字段、非法 enum/ID/URI、禁止控制字符、伪装成 `user_text` 的文件路径和跨字段矛盾；不得静默裁剪后报告成功。
- **FR-A1-007**：validate 响应不得回显完整 candidate、脚本文本、立场、reference locator、绝对输入文件路径或未知字段；只返回 schema identity、canonical candidate digest（仅结构可 canonicalize 时）、`material_constraint_projection_digest_v1`（仅 schema-valid 时）、四轴、计数/field-presence 摘要与有界诊断。
- **FR-A1-008**：Shadow 只读取指定 project 的数字最高 legacy brief revision；先校验 SQL 选中的 project/revision row、原始 `brief_json` bytes 的 stored SHA-256 与 strict JSON object，再执行深层 allowlist 投影。legacy JSON 本身不含 project/revision identity，系统不得伪称完成了不存在的 JSON↔row identity 校验；latest 损坏时不得回退较旧 revision。
- **FR-A1-009**：Shadow 仅映射现有字段能证明的 goal/audience/platform、tone/pace/narrative arc、duration/aspect、must include/exclude、assumptions/missing assets 和严格 evidence refs；不得返回 raw candidate objects、`brief_json`、provenance、provider/model 信息、source path 或自由 locator。
- **FR-A1-010**：Shadow 必须用根 `project_id` 与 `base.legacy_brief={revision,content_sha256}` 联合固定 exact legacy brief；只有 latest observed Timeline 的 row/payload schema version 为受支持的 Timeline 1.0、stored digest 通过、纯 `validate_timeline` 通过、persisted validation status 有效且绑定同一 brief 时，才可加入 `base.observed_timeline={timeline_id,revision,content_sha256}`。它仍必须标记为 observed，而不是 authoritative project head。
- **FR-A1-011**：legacy 中不存在的 stance、script blocks、reference refs、technique refs、Wiki pin 和 user confirmation 必须保持 null/empty 并进入明确 projection/validation gap；不得从 title、goal、narrative、候选 payload或 provenance 推断。
- **FR-A1-012**：Shadow 必须输出 `is_authoritative=false`、`is_persisted=false`、`authority_verified=false` 和 `projection_mode=legacy_shadow_v1`；A0 的 `creative_blueprint_unavailable` gap 继续成立，不能因 shadow 出现而删除。
- **FR-A1-013**：validate 的 base/reference 验证与 shadow 的 project/brief/timeline 读取，在需要数据库时每次调用最多使用一个私有 SQLite snapshot；同一响应不得拼接两个时刻的 current state。Project、Media evidence 与 confirmed Creator Memory 必须通过各自 connection-aware proof reader 验证，Blueprint 不得复制弱化的平行 SQL 规则。
- **FR-A1-014**：两个操作 effect class 都是 read-only/pure validation；不得改写 source SQLite/WAL/SHM 或任何持久化用户/项目状态，不得创建 Blueprint/project/revision、修改 project head、写 cache/project 文件、扫描 Library、打开原媒体、调用模型、DNS/socket 或解析 reference locator。共同只读层可以在系统临时目录创建并自动清理私有磁盘 SQLite snapshot；因此 safety 必须如实返回 `candidate_ephemeral=true`、`private_ephemeral_snapshot_possible=true`、`persistent_state_written=false`，不得声称整个操作 `in_memory_only`。即使 `MEMOLENS_PLUGIN_TRUST_LOCAL_API=1`，Gateway/CLI/MCP 启动和这两个 Blueprint 操作也必须始终走 SQLite 读模型且零 DNS/socket；HTTP client 只能在其他明确需要 API 的操作中 lazy initialize。
- **FR-A1-015**：所有外部文本按 untrusted data 处理；错误 code/path/message 不得包含输入值、未知 key、文件路径、provider payload、prompt、script、locator 或数据库 locator。响应递归隐私扫描泄漏必须为 0。
- **FR-A1-016**：schema-valid、base-verified、references-verified、planning-ready 和 authority-verified 必须保持独立；任何一个 true/status 不得隐式升级其他轴或生成确认、写入、Timeline、素材选择。
- **FR-A1-017**：旧数据库缺 project/brief 能力、需要的 snapshot/query 失败，或 bundled schema 缺失/digest 不符时必须返回结构化 `capability_unavailable|incomplete`；行不存在、能力不可用与内容冲突不得互相伪装。Schema 完整性失败只关闭两个 Blueprint capability，已有 Wiki/Timeline/A0 读取能力不得回退，shadow 不建立平行数据库真源。
- **FR-A1-018**：candidate canonical digest 只用于同内容比较，不是签名、approval、authority proof 或 persisted revision identity；字段顺序不同但语义相同的有效 candidate 必须得到相同 digest。
- **FR-A1-019**：Creator Memory binding 只有在 canonical Creator reader 证明 relation capability 完整、source 属于 confirmed allowlist、profile JSON 的实际 canonical digest 与 pin 相符、profile 字段合法且 evidence refs 存在时才是 resolved。Legacy shadow 中无法完整证明的 creator pin 必须置 null 并返回 gap。

## Contract Summary

| 操作 | 成功对象 | 输入 | 输出重点 |
| --- | --- | --- | --- |
| `blueprint-shadow` / `memolens_blueprint_shadow` | `memolens.creative_blueprint_shadow` | strict project ID | legacy source binding、非权威 candidate、四轴结果、projection gaps |
| `blueprint-validate` / `memolens_blueprint_validate` | `memolens.creative_blueprint_validation` | candidate/v1 JSON/object | schema identity/digest、candidate digest、四轴、有界摘要/诊断 |

两个响应都必须包含 schema identity、mode、safety 和 authority；Shadow 返回 projection gaps，Validate 在 `planning_readiness.gaps` 返回规划缺口。Validate 不返回 candidate；Shadow 返回的 candidate 已经过 allowlist projection，但仍标为 untrusted、non-authoritative data。

## Success Criteria

- **SC-A1-001**：同一有效 candidate 经 Gateway、CLI file、CLI stdin 和 MCP 得到相同 schema digest、candidate digest 与四轴领域结果，一致率 100%。
- **SC-A1-002**：golden legacy fixture 的所有可证明字段映射正确率 100%；stance/script/reference/technique/user confirmation 伪造数为 0；所有 shadow 均明确 non-authoritative/non-persisted。
- **SC-A1-003**：latest brief/Timeline 的 digest mismatch、invalid/duplicate JSON、Timeline row/payload/brief-provenance 矛盾或资源超限均不回退旧 revision，不输出候选内容派生摘要；exact row absent、exact ref stale、digest conflict 与 query/capability failure 分类正确。
- **SC-A1-004**：duplicate key、NaN/Infinity、invalid UTF-8、控制字符、过大整数、transport/canonical bytes、depth/nodes/member/item/string 的 actual limit-1/limit/limit+1、direct MCP object cycle/unsupported type 和 bool-as-int 对抗测试全部 fail closed；524,289-byte 的 raw padding/escaped-equivalent 在 canonical candidate 未超限时必须在 CLI/MCP 得到相同领域结果。
- **SC-A1-005**：四轴矩阵覆盖每个枚举值；schema-valid 但 authority 未验证、base stale/conflict、reference partial/unresolved、planning blocked 等正交组合无误报。
- **SC-A1-006**：所有新路径在单 snapshot、API trust=0/1 都无 DNS/socket、无模型、Library 根下任意媒体 read-open/os.open 与 scandir/listdir/glob/rglob 都 fail-fast、原 DB/WAL/SHM 和项目目录 bytes 不变测试中通过。
- **SC-A1-007**：递归扫描成功/错误响应，candidate/script/stance/locator/绝对路径/provider/raw brief/provenance/data URL 泄漏为 0；errors 永不超过 64 且 truncation 诚实。
- **SC-A1-008**：现有 A0 project resume、Wiki、Timeline、plugin、backend、Node、renderer、typecheck 和 lint 回归通过后，才允许把本切片状态改为 `IMPLEMENTED / VALIDATED`。

## Non-Goals

- 不新增或修改 SQLite schema，不持久化 candidate，不创建 Creative Blueprint revision。
- 不实现 Blueprint create/update/commit、typed operation、CAS、idempotency、undo/redo、branch 或 authoritative project head；这些属于 ML-015-B 或后续独立切片。
- 不改变 legacy brief 的生产写路径，不把 legacy brief 或 shadow 重命名为真正 Creative Blueprint。
- 不生成脚本、素材 coverage、Timeline、字幕、preview、render 或 export。
- 不实现 Creator Memory/Wiki/Craft Wiki 写入、generation pinning、Technique Card 编译或 reference 网络抓取。
- 不增加 App UI、聊天、内置模型、provider key、Agent scheduler 或 schema 下载/协商服务。
- 不验证用户真实身份或确认，不把 `caller_declared_user_input` 当作 `user_confirmed`。
- 不返回 raw schema dump、raw candidate、raw brief、raw Timeline、raw provenance、原媒体或文件系统 locator。

## Rollback

本切片只增加版本化 schema 工件、纯 validator、legacy shadow projector 和两个只读入口。回滚时移除新入口/模块与 status capability metadata 即可；没有 migration、持久化 Blueprint、项目 revision、原件或用户文件需要恢复。已经存在的 A0 project resume、Wiki 和 Timeline 读表面必须保持可用。

## Implementation Evidence

- checked-in `creative-blueprint-candidate-v1.schema.json`、runtime validator 和 MCP shadow output contract 使用同一 closed-world schema；exact-byte SHA-256 为 `9f96c78586eea1c9a21bb77037e8a59a2f6fc5b7701df91c5f769ec7f366bdde`，status 通过 `creative_blueprint_contract` 公开同一 version/digest。
- `MemoLensGateway`、CLI 和 MCP 已共同交付 `blueprint-shadow` / `blueprint-validate`；CLI/MCP 只负责 strict transport，schema、cross-field、digest、四轴和 shadow projection 由同一领域内核判定。
- Legacy shadow 只投影 digest 正确的数字最高 brief，并仅在 Timeline row/payload/schema/digest/validation status/brief provenance 全部一致时加入 observed Timeline；duplicate、malformed、resource overflow 或 conflict 均不回退旧 revision。
- exact base 查询区分 `missing`、`stale`、`conflict` 与 query/capability failure；Project、Media evidence 与 confirmed Creator binding 复用 connection-aware proof reader，单次绑定操作只使用一个私有 SQLite snapshot。
- Blueprint safety 不再声称 `in_memory_only`：响应明确 `candidate_ephemeral=true`、`private_ephemeral_snapshot_possible=true`、`persistent_state_written=false`；source DB/WAL/SHM、项目、Library 和原媒体保持不变。
- 61 个 A1 专项测试通过，覆盖 Gateway/CLI file/CLI stdin/MCP parity、四轴矩阵、authority、schema damage 局部降级、HTTPS/user-text locator、actual resource ceilings、raw MCP exact/+1/drain/duplicate/NaN/UTF-8、隐私、单 snapshot、零网络/媒体读取和 persisted latest no-fallback。
- 插件全量 170/170、后端 104/104、Node 51/51、renderer 43/43 通过；TypeScript typecheck、production build、本地端到端验证、全仓 Ruff、Python compile、manifest/schema JSON、schema digest 和 `git diff --check` 全部通过。
- 独立代码合同复审与独立安全复审均在修复所有发现后重跑 A1 61/61，并确认当前共享树无未修复 P0/P1/P2，准许标记 `IMPLEMENTED / VALIDATED`。
- 未增加 SQLite migration、Blueprint/project 写入、App UI、模型/网络调用、reference 抓取、Library 扫描、原媒体打开、Timeline 生成、渲染、导出或用户文件改动。
