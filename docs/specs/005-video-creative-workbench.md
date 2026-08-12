# Spec 005：MemoLens Video Creative Workbench

- 状态：`PROPOSED`（不表示已实现或可发布）
- 产品能力代号：MemoLens `0.2.0` Video Creative Workbench
- 实际发布版本：`TBD`；由于仓库已有不可变 `v0.2.0` 标签，实现产物 **MUST** 使用更高的 SemVer（预期 `0.3.0`），**MUST NOT** 重打、移动或覆盖 `v0.2.0`
- API 版本：`v1` 路径不变；响应增加 `schema_version`
- 数据库目标版本：`2`
- 负责人：MemoLens maintainers
- 最后更新：2026-08-12
- 依赖规范：Spec 004《MemoLens Evidence-Backed Retrieval & Privacy Benchmark》（RC 前 **MUST** 固定其仓库路径与 commit SHA）

本文使用 RFC 2119 风格术语：**MUST（必须）**、**MUST NOT（禁止）**、**SHOULD（应该）**、**SHOULD NOT（不应该）**、**MAY（可以）**。

## 1. 摘要

MemoLens 0.2.0 Video Creative Workbench 将现有“本地图片索引与检索”扩展为一个端到端、可审计的视频创作工作台：

1. **Memory**：长期理解图片与视频；视频以带时间边界的 `VideoSegment` 为主要检索单元。
2. **Director**：把用户创作意图转成 creative brief、故事板、素材建议和缺失素材列表。
3. **Editor**：把故事板转成确定性的 timeline JSON，支持对话式修订、本地预览和 MP4 导出。

产品与 ChatCut 类产品的共同点是“使用自然语言生成并修改剪辑”；MemoLens 的差异化 **MUST** 是：素材在项目之外长期存在、提前索引，并允许跨历史图片和视频片段进行创作。MemoLens 不应退化成“上传本次素材后生成一次性初剪”的会话工具。

MVP **MUST** 是真实可运行闭环，而不是空 UI：

```text
导入图片/视频
  → 本地探测与自适应抽帧
  → 片段级索引（转写可选）
  → 图片 + VideoSegment 混合检索
  → creative brief
  → 确定性 timeline JSON
  → 校验
  → FFmpeg 本地预览
  → MP4 导出
```

## 2. 背景与问题

现有 MemoLens 已具备 Electron/React 桌面端、Flask loopback API、SQLite `image_index`、图片索引、检索、Atlas、Workbench，以及默认离线只读的 Codex 插件。当前缺口不是另一个“视频摘要”入口，而是：

- 图片与视频无法作为同一素材库被长期记忆和检索；
- 视频没有片段级、时间对齐的视觉/文本描述；
- 检索结果不能落到可执行剪辑范围；
- 创意建议不能生成可验证、可修订、可渲染的时间线；
- Codex 不能在安全默认下操作创作项目。

视频理解 **MUST NOT** 被描述为逐帧无遗漏。MVP 采用“粗看 → 判断 → 定向回看 → 精剪”的粗到细策略，在成本、时延和召回之间做明确取舍。

## 3. 产品定位与成功定义

### 3.1 Memory

Memory **MUST**：

- 导入受支持的图片和视频，不修改原始文件；
- 使用 `ffprobe` 记录媒体元数据；
- 将视频分解为有时间范围的 `VideoSegment`；
- 将关键帧、带时间戳转写（若启用）、OCR/视觉描述与片段对齐；
- 把 `ImageAsset` 与 `VideoSegment` 暴露为统一检索结果；
- 允许后续查询触发候选区间的定向回看。

### 3.2 Director

Director **MUST**：

- 接收目标、受众、平台、时长、画幅、基调、节奏、必须包含/排除条件；
- 检索已有素材并引用稳定 `asset_id` / `segment_id`；
- 输出故事结构、镜头建议、选材理由和缺失素材；
- 清楚区分“库存中已找到”“推断可用”“尚缺失”；
- 不得杜撰不存在的素材或时间范围。

### 3.3 Editor

Editor **MUST**：

- 从 brief 创建结构化 timeline；
- 支持替换、移动、裁剪、延长、删除、调音量、改画幅、改文字等确定性修订；
- 每次修订创建新 revision，并保留 provenance；
- 在渲染前验证路径、时长、轨道与格式；
- 用本地 FFmpeg 生成低成本预览和 MP4 导出；
- 支持取消、失败恢复和重试。

### 3.4 非目标

第一阶段明确 **不做**：

- 逐帧无遗漏的视频理解承诺；
- 精确人体姿态、动作捕捉、目标跟踪和身份重识别；
- 专业 NLE 的任意嵌套序列、关键帧曲线、调色节点和插件生态；
- Premiere/Final Cut/DaVinci 原生项目文件的完整双向兼容；
- 自动获取带版权限制的配乐库；
- 自动发布到社交平台；
- 生成式补帧、视频生成、口型同步和高级视觉特效；
- 多用户协作、云同步或远程渲染农场。

## 4. 约束与架构原则

1. 现有 `image_index` **MUST** 向后兼容；已有数据库升级后图片检索结果不得丢失。
2. 优先使用 Python 标准库、SQLite、FFmpeg/ffprobe 与现有 Flask/React/Electron 架构。
3. MVP **MUST NOT** 引入 Celery、Redis、Kafka、独立向量数据库或大型 NLE 框架。
4. 长任务状态 **MUST** 持久化到 SQLite；进程重启后可判定并恢复或安全失败。
5. 所有媒体操作 **MUST** 非破坏性；原始素材不得被覆盖、移动或删除。
6. 所有写入输出 **MUST** 限制在用户选定且已批准的项目/导出目录。
7. 默认网络策略 **MUST** 为离线；外部 VLM 或转写 **MUST** 显式 opt-in。
8. Timeline **MUST** 是最终事实来源；模型自然语言和自由式 EDL 不得直接驱动 FFmpeg。
9. 文件内容身份与文件所在路径 **MUST** 分离；同一内容在多个已批准 root 下只是一个 `asset`，但可有多个 `asset_source`。
10. 分析、时间线与渲染产物 **MUST** 不可变地绑定其输入 revision、内容 hash 和原数据库实例；切换当前数据库不得重绑已有任务。
11. 环境中存在外部 provider 的 API key 、已启用图片分析或设置了 `MEMOLENS_PLUGIN_TRUST_LOCAL_API=1`，均 **MUST NOT** 被解释为视频帧、音频或转写文本出境授权。

## 5. 端到端系统设计

```text
Electron / React
  Setup → Library → Create → Storyboard/Timeline → Preview/Export
        │
        ▼
Flask loopback API v1
  media jobs / retrieval / director / timeline / render
        │
        ├── SQLite schema v2
        ├── Local job runner（有界线程/子进程）
        ├── ffprobe / ffmpeg
        ├── 本地图像变化、去重、清晰度与代表帧选择
        ├── 可选本地/外部转写与 VLM adapter
        └── 现有检索、embedding 与 Codex plugin
```

### 5.1 任务执行器

实现 **SHOULD** 使用现有 Python 进程内有界任务执行器与 `subprocess.Popen`，而不是新增队列服务：

- 同时最多 1 个渲染任务、1 个视频索引任务；图片索引沿用现有机制。
- 状态机：`queued → running → succeeded | partial | failed | cancelling | cancelled`；进程异常退出后的 `queued/running/cancelling` 转为 `interrupted`，`interrupted → queued` 只能由显式 resume 发生。
- 所有阶段 **MUST** 持久化 `stage`、`progress`、`attempt`、`last_error` 和心跳。
- `progress` **MUST** 在单次 attempt 内单调不降且仅取 `[0,1]`；`stage` 变更、终态和最后 64 KiB 脱敏诊断必须在同一事务中落库。
- 取消 **MUST** 先持久化 `cancel_requested=1` 并返回 `202 cancelling`，再终止当前 FFmpeg/转写子进程及其进程组；先 `terminate`，3 s 超时后才 `kill`。重复取消返回同一终态，不生成新任务。
- 应用重启时，遗留 `running` 任务 **MUST** 变为 `interrupted`，随后按任务类型显示“恢复”或“重新开始”。
- 可恢复步骤 **MUST** 通过内容 hash、分析 profile version 和阶段工件 hash 跳过已完成工作；工件任一校验不符必须重做该阶段。
- 每个 job 在创建时 **MUST** 捕获 `database_uuid` 和内部 `DatabaseBinding`；worker 不得在后续阶段重读“当前数据库”设置。用户在 DB A 有运行任务时切到 DB B，A 的任务仍必须只读写 A。
- app-state 中的本地 job route registry **MUST** 持久化 `job_id → database_uuid → database locator`，仅供后端恢复和路由；API 与日志不得暴露 locator。若原 DB 不可达，状态必须是 `blocked_source_unavailable`，不得改写当前 DB。

## 6. 媒体导入与探测

### 6.1 支持平台

P0 发布支持：

- macOS 13+，Apple Silicon；
- Python 3.10+；Node 22.12+；
- FFmpeg/ffprobe 6+，同一发行包且版本可探测。Phase 1 明确选择“用户/系统安装的本地二进制”，不在应用内自动下载 FFmpeg；打包版发布前 **MUST** 记录已验证的绝对可执行文件、版本、构建配置和 license notice。开发环境可从 `PATH` 探测，生产运行后不得在 job 中悄然切换二进制。

P1 SHOULD 覆盖 Windows 11。Linux 可开发运行，但在未完成打包验收前不得声称正式支持。

### 6.2 支持格式

P0 输入：

- 图片：现有 JPG/JPEG、PNG、WEBP、BMP、GIF、TIFF、HEIC/HEIF 能力；
- 视频容器：MP4、MOV、M4V；
- 视频编码：H.264、HEVC（依赖本机 FFmpeg decoder）；
- 音频：视频内 AAC/PCM/MP3 音轨，及独立 WAV、MP3、M4A（仅 timeline 音频轨）。

P0 输出：MP4（H.264 + AAC，`yuv420p`）。无对应 decoder/encoder 时 **MUST** 返回能力错误，不得静默降级为损坏文件。

补充格式语义：动画 GIF/多页 TIFF 在 P0 仅按第一帧静态图片处理，UI **MUST** 显示警告；视频 rotation metadata **MUST** 被显式应用；VFR 输入保留 source timestamp，输出统一为 timeline 指定的 CFR。音频或视频有多条默认 stream 时，P0 选择第一条 disposition `default=1`，否则选择最低 stream index，并将选择写入 provenance。

### 6.3 ffprobe

每个视频导入时 **MUST** 使用参数数组而非 shell 字符串运行：

```text
ffprobe -v error -print_format json -show_format -show_streams <absolute-path>
```

探测结果 **MUST** 记录：duration、stream index、codec、width、height、rotation、average frame rate、time base、pixel format、audio sample rate/channels、creation time（若可信）。

系统 **MUST** 拒绝：

- 路径不在已批准 library roots 内；
- 软链接解析后逃逸根目录；
- 非普通文件；
- 声称格式与实际探测不符；
- duration 非有限值或 ≤ 0；
- 超过可配置安全上限（默认单文件 8 小时或 100 GB）。

`library_root` 只能由 Electron main process 的原生目录选择器或明确的本地管理员 CLI 注册。Renderer、Codex 和普通 API 请求只能传 `library_root_id + relative_path`。root 注册时 **MUST** 保存 canonical path 及 permission fingerprint；每次读取前必须重新解析并确认目标仍在 root 内。P0 对素材文件和中间路径中的 symlink **MUST** fail closed；不允许 URL、管道、device file 或 FFmpeg 网络协议作为素材输入。

## 7. 粗到细视频理解策略

### 7.1 原则

固定每秒一帧和每 30 秒一帧都不是默认方案。编码 I-frame **MUST NOT** 被等同于语义关键帧。系统 **MUST** 先用本地廉价信号生成候选，再将少量代表帧交给视觉理解。

### 7.2 Pass A：本地廉价扫描

Pass A **MUST** 在不调用外部模型的情况下完成：

1. 使用 FFmpeg 生成低分辨率扫描帧；默认 `4 fps`、长边不超过 `320 px`。
2. 计算相邻帧亮度/颜色直方图差异；实现 MAY 使用标准库 + Pillow/Numpy（现有依赖）而非大型 CV 套件。
3. 检测候选镜头边界：硬切、明显淡入淡出、黑场后恢复。
4. 计算清晰度、黑屏、重复度和运动变化近似分数。
5. 结合音频静音/响度边界（FFmpeg `silencedetect` / `astats`）生成额外候选时间点。
6. 对超过默认 `5 s` 未产生候选的区间添加保底采样点。

默认参数必须进入可版本化 `analysis_profile`：

```json
{
  "id": "adaptive-v1",
  "scan_fps": 4,
  "scan_max_edge": 320,
  "fallback_interval_ms": 5000,
  "min_shot_ms": 500,
  "max_shot_ms": 30000,
  "representatives_per_shot_min": 1,
  "representatives_per_shot_max": 4
}
```

### 7.3 Pass B：语义代表帧

每个候选镜头 **MUST** 形成一个 `VideoSegment`。代表帧选择 **MUST**：

- 避开转场边界和模糊帧；
- 优先主体完整、清晰、接近镜头视觉中心的帧；
- 短镜头通常选 1 帧，长镜头或内部变化明显时选 2–4 帧；
- 记录选择原因与分数，不得只保存文件路径；
- 每个片段至少有 1 个可用代表帧；若无可用帧则标记 `visual_status=degraded`。

视觉理解提供器收到的是代表帧、片段时间和必要上下文。输出 **MUST** 为结构化字段：subjects、objects、actions、setting、shot_type、camera_motion、mood、colors、visible_text、quality_notes、summary、confidence。无 VLM 时使用本地 fallback：文件/时间/技术元数据、OCR（仅在已有本地能力可用时）和“未进行语义视觉分析”状态，**MUST NOT** 伪造描述。

### 7.4 Pass C：按查询定向回看

混合检索或 Director 遇到以下情况时 SHOULD 创建 `refinement_job`：

- 候选片段语义相关但 confidence 低；
- 用户要求短暂动作、界面变化或具体对象；
- 转写和代表帧信息冲突；
- 候选片段长度 > 15 秒且内部变化高；
- 多个结果分数接近且会影响剪辑选材。

定向回看范围 **MUST** 有界：默认候选时间前后各 2 秒、最高 5 fps、单次最多 60 秒、最多 300 帧；超过预算时返回 `refinement_budget_exceeded` 并请求用户缩小范围。回看结果作为新的、不可变的完整 `analysis_run` revision 保存，不原地覆盖原分析。为避免“一半新、一半旧”的查询快照，MVP **MUST** 物化该资产的完整新 revision（未变片段可复用同 hash 缓存）；只有 run 全部校验成功后才能在单个事务中替换 current-successful head。

同一混合检索响应 **MUST** 绑定一个一致的 `analysis_run_id` 集合。后台 refinement 完成后必须通过新的 search revision/响应返回，不得在客户端已看到的结果中静默替换时间范围。

### 7.5 不可承诺

产品文案、API 和发布说明 **MUST NOT** 声称：

- 理解视频的每一帧；
- 不遗漏小于采样间隔的事件；
- 精确识别人物身份、姿态或所有对象；
- 能从关键帧可靠推断所有运动方向和因果；
- 对任意一小时视频保持完整、无误的跨时段记忆。

## 8. 时间戳音频转写

### 8.1 能力

转写是可选能力。启用时 **MUST** 产生句级时间戳；词级时间戳 MAY 提供。每条记录包含 `start_ms`、`end_ms`、`text`、`speaker_label?`、`confidence?`、`provider`、`model`。

### 8.2 默认与 fallback

- 默认离线配置 **MUST NOT** 要求第三方 API key。
- 如果没有可用本地转写模型，索引仍 **MUST** 完成，状态为 `transcript_status=unavailable`。
- 无转写时，Director **MUST** 明确提示“本次仅依据画面和技术元数据，无法判断完整对白内容”。
- 有嵌入字幕时 MAY 本地提取作为 `source=embedded_subtitle`。
- 转写失败 **MUST** 形成部分成功，而不是让整个视频索引失败。

Phase 1 **MUST** 实现一个真实可运行的本地时间戳转写 adapter，默认选用“用户安装的 `whisper.cpp` 兼容 CLI + 用户选定的本地模型”；MemoLens 不自动下载二进制或模型。Setup 必须探测 adapter 版本与模型文件，adapter 必须把音频输出到 job 专属临时目录，返回可 schema-validate 的句级时间戳 JSON。P0 验收必须用合成语音 fixture 完成一次真实转写；未安装时同样必须验收 fallback。外部转写 provider MAY 后续增加，但不是 Phase 1 闭环前提。

“本地转写未安装”是预期降级：整个分析 run 仍可标记 `succeeded`，而 asset 的 `transcript_status=unavailable`。只有必需的本地分镜/代表帧失败才使 run 成为 `partial/failed`；这一取舍保证“current successful revision”不会因可选能力缺席而永远为空。

### 8.3 时间对齐

`TranscriptSegment` 与 `VideoSegment` 通过时间重叠关联，不复制成唯一事实。检索时组合文本 SHOULD 包含与片段重叠的转写；展示必须保留原始时间戳。

转写 adapter 输出必须满足 `0 <= start_ms < end_ms <= asset.duration_ms + 1 frame`，按 `(start_ms,end_ms,id)` 稳定排序；重叠可保留，但空文本、NaN confidence 或时间倒置必须拒绝。转写原文不得写入普通日志。

## 9. 数据模型与幂等迁移

本节 SQL 是 P0 最低物理契约，不是仅供参考的伪代码。实现 MAY 增加列和索引，但不得削弱下述身份、外键、不可变 revision 与 root/grant 约束。所有 ID 使用带类型前缀的 UUID4（例如 `ast_...`、`seg_...`、`tl_...`）；时间为 UTC RFC 3339，hash 为小写十六进制 SHA-256。

### 9.1 SQLite 连接与迁移不变量

每个 backend、worker、测试和 Codex 只读插件打开 SQLite 后 **MUST** 统一执行/验证：

```sql
PRAGMA foreign_keys = ON;   -- 必须回读为 1
PRAGMA busy_timeout = 5000;
PRAGMA journal_mode;        -- 必须回读为 wal
```

可写连接在数据库初始化/迁移事务之前还 **MUST** 执行 `PRAGMA journal_mode=WAL` 并确认返回 `wal`，使用 `PRAGMA synchronous=NORMAL`。严格只读连接不得尝试改写 journal mode，但必须 fail closed 于非 WAL 数据库。无法提供 WAL 锁语义的网络文件系统不在 P0 支持范围，不得静默降级到 DELETE journal。

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  checksum TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS database_meta (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  database_uuid TEXT NOT NULL UNIQUE,
  schema_version INTEGER NOT NULL CHECK(schema_version >= 2),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

迁移顺序与恢复契约：

1. 当前受支持的 `image_index` 视为 schema v1。只有其列/索引 fingerprint 匹配已冻结 v1 清单时，才写入 baseline；未知变体必须返回 `unsupported_legacy_schema`，不得猜测修补。
2. 迁移前 **MUST** 做 WAL checkpoint，在 app-state backup 目录创建 SQLite backup API 快照及 SHA manifest，预检至少 `max(512 MiB, 2×DB size)` 可用空间。
3. 每个 migration **MUST** 在单个 `BEGIN IMMEDIATE` 事务中执行；失败后回滚并保留旧 DB。重复启动必须幂等；同 version checksum 不同必须阻止写入并返回 `migration_checksum_mismatch`。
4. v2 migration **MUST NOT** 删除、重命、改类型或重建 `image_index`。迁移前后必须比对其行数、`id`、`sha256`、`relative_path` 集合与内容 digest。
5. DDL 迁移与大量回填分离。回填使用 `media_jobs(kind='legacy_image_backfill')` 分批 checkpoint；未完成时旧图片检索继续从 `image_index` 读取，完成后进行集合等价校验。
6. v2 上线后所有旧图片索引写路径 **MUST** 在同一事务中 dual-write `image_index` 与 `assets/asset_sources`；只读旧 API 的字段与语义至少保留一个完整发布周期。
7. 回退旧应用只能发生在未产生 v2 写入或从迁移前快照恢复后；不提供会丢失 v2 项目/时间线的“反向 migration”。

### 9.2 Root、内容资产与多路径来源

```sql
CREATE TABLE library_roots (
  id TEXT PRIMARY KEY,
  canonical_path TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  permission_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active','unavailable','revoked')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE assets (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('image','video','audio')),
  sha256 TEXT NOT NULL UNIQUE,
  mime_type TEXT NOT NULL,
  file_size INTEGER NOT NULL CHECK(file_size >= 0),
  duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms > 0),
  width INTEGER CHECK(width IS NULL OR width > 0),
  height INTEGER CHECK(height IS NULL OR height > 0),
  rotation_degrees INTEGER,
  codec_json TEXT NOT NULL DEFAULT '{}',
  captured_at TEXT,
  probe_status TEXT NOT NULL CHECK(probe_status IN ('pending','ready','unsupported','failed')),
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE asset_sources (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
  library_root_id TEXT NOT NULL REFERENCES library_roots(id) ON DELETE RESTRICT,
  relative_path TEXT NOT NULL,
  display_filename TEXT NOT NULL,
  observed_size INTEGER NOT NULL CHECK(observed_size >= 0),
  observed_mtime_ns INTEGER,
  source_file_id TEXT,
  availability TEXT NOT NULL CHECK(availability IN ('available','missing','changed','revoked')),
  is_preferred INTEGER NOT NULL DEFAULT 0 CHECK(is_preferred IN (0,1)),
  last_verified_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(library_root_id, relative_path)
);
CREATE INDEX idx_assets_kind_probe ON assets(kind, probe_status);
CREATE INDEX idx_assets_captured_at ON assets(captured_at);
CREATE INDEX idx_asset_sources_asset_availability ON asset_sources(asset_id, availability);
CREATE UNIQUE INDEX idx_asset_sources_one_preferred
  ON asset_sources(asset_id) WHERE is_preferred = 1;
```

`assets` 是不可变的内容身份，`asset_sources` 是可变的所在位置。同 SHA 的两个路径 **MUST** 映射到同一 `asset_id` 和两个 `source_id`。若原路径的内容变了，系统必须把该 source 标记 `changed`，为新 hash 创建/关联新 asset；不得改写旧 `assets.sha256`。缺失文件只改 source availability，不删除长期记忆或时间线引用。

Timeline 必须同时引用 `asset_id` 和一个 `asset_source_id`。该 source 不可用时，validator 返回 `source_unavailable` 及同 hash 可用替代 source ID；只有显式 `relink_source` typed operation 能创建使用替代 source 的新 timeline revision，渲染器不得自行换路径。渲染前重验 size/mtime，有变化时重算 SHA；不匹配即 `source_changed`。

现有图片回填时 `assets.id` **MUST** 优先保留 `image_index.id`，并将现有 configured image library 注册为第一个 `library_root`。如任一 legacy `relative_path` 在 canonical root 外、冲突或无法唯一映射，回填必须停止并列出冲突，不得丢行。

### 9.3 分析 run、当前成功 revision 与片段

```sql
CREATE TABLE analysis_runs (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
  revision INTEGER NOT NULL CHECK(revision >= 1),
  run_kind TEXT NOT NULL CHECK(run_kind IN ('initial','reanalyze','refinement')),
  parent_run_id TEXT REFERENCES analysis_runs(id) ON DELETE RESTRICT,
  analysis_profile_id TEXT NOT NULL,
  analysis_profile_json TEXT NOT NULL,
  input_asset_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('queued','running','succeeded','partial','failed','cancelling','cancelled','interrupted')),
  transcript_status TEXT NOT NULL,
  visual_status TEXT NOT NULL,
  error_json TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE(asset_id, revision),
  UNIQUE(asset_id, id)
);

CREATE TABLE asset_analysis_heads (
  asset_id TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
  analysis_run_id TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(asset_id, analysis_run_id)
    REFERENCES analysis_runs(asset_id, id) ON DELETE RESTRICT
);

CREATE TABLE video_segments (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  analysis_run_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
  end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
  boundary_reason TEXT NOT NULL,
  summary TEXT,
  semantic_json TEXT NOT NULL DEFAULT '{}',
  visible_text TEXT,
  combined_text TEXT NOT NULL DEFAULT '',
  text_embedding_model TEXT,
  text_embedding BLOB,
  visual_status TEXT NOT NULL,
  transcript_status TEXT NOT NULL,
  confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  created_at TEXT NOT NULL,
  FOREIGN KEY(asset_id, analysis_run_id)
    REFERENCES analysis_runs(asset_id, id) ON DELETE CASCADE,
  UNIQUE(analysis_run_id, ordinal)
);
CREATE INDEX idx_video_segments_run_time ON video_segments(analysis_run_id, start_ms, end_ms);
CREATE INDEX idx_video_segments_asset_time ON video_segments(asset_id, start_ms, end_ms);

CREATE TABLE keyframes (
  id TEXT PRIMARY KEY,
  segment_id TEXT NOT NULL REFERENCES video_segments(id) ON DELETE CASCADE,
  timestamp_ms INTEGER NOT NULL CHECK(timestamp_ms >= 0),
  cache_key TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  width INTEGER NOT NULL CHECK(width > 0),
  height INTEGER NOT NULL CHECK(height > 0),
  selection_reason TEXT NOT NULL,
  clarity_score REAL,
  novelty_score REAL,
  is_representative INTEGER NOT NULL CHECK(is_representative IN (0,1)),
  created_at TEXT NOT NULL,
  UNIQUE(segment_id, timestamp_ms, sha256)
);
CREATE INDEX idx_keyframes_segment ON keyframes(segment_id, is_representative, timestamp_ms);

CREATE TABLE transcript_segments (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  analysis_run_id TEXT NOT NULL,
  start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
  end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
  text TEXT NOT NULL CHECK(length(trim(text)) > 0),
  language TEXT,
  speaker_label TEXT,
  confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  source TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT,
  payload_manifest_id TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(asset_id, analysis_run_id)
    REFERENCES analysis_runs(asset_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_transcript_run_time ON transcript_segments(analysis_run_id, start_ms, end_ms);
```

`asset_analysis_heads` 是唯一 current selector；查询 **MUST NOT** 使用 `MAX(revision)`，因为更高 revision 可能失败或被取消。只有 `analysis_runs.status='succeeded'`、输入 SHA 仍匹配且片段/关键帧约束验证通过后，才能于单事务替换 head。`partial/failed/cancelled/interrupted` run 保留诊断与已完成工件，但不会遮蔽上一个成功 revision。首次 run 不成功时，Library 可查看局部结果，默认混合检索不纳入它们。

实现 **MUST** 提供只读 `current_video_segments` view，只 join `asset_analysis_heads.analysis_run_id`。每个 revision 是完整物化快照；已用 SHA 定址的 keyframe cache 可复用，但旧 run 的数据行不得原地更新。关键帧缓存位于 app-state cache，不放入 library；缓存删除后可由 `cache_key + analysis_profile` 重建。

### 9.4 Creative brief 与不可变 timeline

```sql
CREATE TABLE creative_projects (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('draft','active','archived')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE creative_briefs (
  project_id TEXT NOT NULL REFERENCES creative_projects(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL CHECK(revision >= 1),
  brief_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  provenance_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(project_id, revision)
);

CREATE TABLE timelines (
  id TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES creative_projects(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL CHECK(revision >= 1),
  parent_revision INTEGER,
  brief_revision INTEGER NOT NULL,
  schema_version TEXT NOT NULL,
  timeline_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  provenance_json TEXT NOT NULL,
  validation_status TEXT NOT NULL CHECK(validation_status IN ('valid','invalid')),
  validation_errors_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  PRIMARY KEY(id, revision),
  FOREIGN KEY(project_id, brief_revision)
    REFERENCES creative_briefs(project_id, revision) ON DELETE RESTRICT,
  FOREIGN KEY(id, parent_revision)
    REFERENCES timelines(id, revision) ON DELETE RESTRICT
);
CREATE INDEX idx_timelines_project_revision ON timelines(project_id, revision DESC);
```

Brief 与 timeline revision 一经写入 **MUST** 不可变。revision 1 的 `parent_revision` 为 `null`；revision N 的 parent 必须是同 timeline 的 N-1。修订使用 optimistic concurrency：客户端提交 `base_revision`，不是当前 head 则返回 `409 revision_conflict`，不自动 merge。

### 9.5 Output root、短期 grant 与任务

```sql
CREATE TABLE output_roots (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('app_preview','user_export')),
  canonical_path TEXT NOT NULL UNIQUE,
  permission_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active','unavailable','revoked')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE export_grants (
  id TEXT PRIMARY KEY,
  output_root_id TEXT NOT NULL REFERENCES output_roots(id) ON DELETE RESTRICT,
  project_id TEXT NOT NULL REFERENCES creative_projects(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  token_sha256 TEXT NOT NULL UNIQUE,
  allow_overwrite INTEGER NOT NULL DEFAULT 0 CHECK(allow_overwrite IN (0,1)),
  single_use INTEGER NOT NULL DEFAULT 1 CHECK(single_use IN (0,1)),
  status TEXT NOT NULL CHECK(status IN ('active','consumed','expired','revoked')),
  issued_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  consumed_at TEXT
);

CREATE TABLE media_jobs (
  id TEXT PRIMARY KEY,
  database_uuid TEXT NOT NULL,
  kind TEXT NOT NULL,
  asset_id TEXT REFERENCES assets(id) ON DELETE RESTRICT,
  analysis_run_id TEXT REFERENCES analysis_runs(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK(status IN
    ('queued','running','succeeded','partial','failed','cancelling','cancelled','interrupted','blocked_source_unavailable')),
  stage TEXT NOT NULL,
  progress REAL NOT NULL DEFAULT 0 CHECK(progress >= 0 AND progress <= 1),
  attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt >= 1),
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
  checkpoint_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  heartbeat_at TEXT,
  finished_at TEXT,
  FOREIGN KEY(database_uuid)
    REFERENCES database_meta(database_uuid) ON DELETE RESTRICT
);
CREATE INDEX idx_media_jobs_status_created ON media_jobs(status, created_at);

CREATE TABLE render_jobs (
  id TEXT PRIMARY KEY,
  database_uuid TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  timeline_revision INTEGER NOT NULL,
  profile TEXT NOT NULL CHECK(profile IN ('preview-low','export-1080p')),
  output_root_id TEXT NOT NULL REFERENCES output_roots(id) ON DELETE RESTRICT,
  export_grant_id TEXT REFERENCES export_grants(id) ON DELETE RESTRICT,
  output_relative_path TEXT NOT NULL,
  timeline_content_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('queued','running','succeeded','failed','cancelling','cancelled','interrupted','blocked_source_unavailable')),
  stage TEXT NOT NULL,
  progress REAL NOT NULL DEFAULT 0 CHECK(progress >= 0 AND progress <= 1),
  attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt >= 1),
  ffmpeg_command_json TEXT,
  ffmpeg_version TEXT,
  output_sha256 TEXT,
  stderr_tail TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
  error_json TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  heartbeat_at TEXT,
  finished_at TEXT,
  FOREIGN KEY(timeline_id, timeline_revision)
    REFERENCES timelines(id, revision) ON DELETE RESTRICT,
  FOREIGN KEY(database_uuid)
    REFERENCES database_meta(database_uuid) ON DELETE RESTRICT
);
CREATE INDEX idx_render_jobs_status_created ON render_jobs(status, created_at);
```

`app_preview` root 由应用创建在 app-state，只能容纳 preview；`export-1080p` **MUST** 绑定 `user_export` root 和未过期 `export_grant_id`。`export_grant` 只能由 Electron main 在原生目录选择/导出确认后签发，绑定 project、canonical output root、单一清洗后文件名、覆盖策略和不超过 10 分钟的 TTL。API 不接受任意绝对输出路径。

渲染开始前必须同时验证 timeline 复合外键、`timeline_content_sha256`、root permission fingerprint、grant 与目标父目录 canonical path。目标必须是 root 下的单层文件名（禁止 `/`、`\\`、`..`、NUL）；P0 拒绝 symlink 父目录。默认 `O_EXCL`/等价语义禁止覆盖；只有 grant 显式 `allow_overwrite=1` 才允许在成功校验后原子替换。临时文件与最终产物必须在同一 output root/文件系统。

### 9.6 API 幂等、provider 授权与 payload manifest

```sql
CREATE TABLE idempotency_records (
  scope TEXT NOT NULL,
  key TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('in_progress','completed','failed')),
  resource_type TEXT,
  resource_id TEXT,
  response_status INTEGER,
  response_json TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  PRIMARY KEY(scope, key)
);

CREATE TABLE provider_opt_in_grants (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  capability TEXT NOT NULL CHECK(capability IN ('video_vlm','transcription','codex_visual_inspection')),
  payload_classes_json TEXT NOT NULL,
  asset_scope_json TEXT NOT NULL,
  token_sha256 TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('active','consumed','expired','revoked')),
  single_use INTEGER NOT NULL DEFAULT 1 CHECK(single_use IN (0,1)),
  issued_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  consumed_at TEXT
);

CREATE TABLE provider_payload_manifests (
  id TEXT PRIMARY KEY,
  job_id TEXT,
  grant_id TEXT NOT NULL REFERENCES provider_opt_in_grants(id) ON DELETE RESTRICT,
  provider TEXT NOT NULL,
  model TEXT,
  capability TEXT NOT NULL,
  payload_classes_json TEXT NOT NULL,
  asset_ids_json TEXT NOT NULL,
  time_ranges_json TEXT NOT NULL,
  payload_sha256_json TEXT NOT NULL,
  planned_bytes INTEGER,
  bytes_sent INTEGER,
  outcome TEXT NOT NULL CHECK(outcome IN ('planned','sent','failed_before_send','failed_after_send','cancelled')),
  retention_policy TEXT,
  retention_evidence TEXT,
  user_opt_in_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  completed_at TEXT
);
```

`scope` **MUST** 包含认证主体/短期 capability ID、HTTP method 和规范化 route template。`request_sha256` 对通过 schema 验证和默认值展开后的 JSON 计算：UTF-8、key 递归排序、无多余空白、禁止 NaN/Infinity，并纳入会影响行为的 query/header 字段。创建资源和写 revision 的 POST **MUST** 要求 `Idempotency-Key`；服务器在 `BEGIN IMMEDIATE` 中先占位：

- 同 scope/key + 同 request hash 且仍在运行：返回同一 resource 的 `202`；
- 同 scope/key + 同 request hash 已完成：重放已存 `response_status/response_json`，不再执行副作用；
- 同 scope/key + 不同 request hash：`409 idempotency_conflict`；
- 并发首次请求只能有一个占位成功。记录默认保留 24 小时，未过期不可清理。

manifest 只保存 payload 类型、hash、字节数和时间范围，不保存媒体或完整转写副本。任何 provider 调用必须先写 `planned` manifest 并验证 grant；发送后再记录实际字节和 outcome。只有环境 key 而无 `provider_opt_in_grant` 时，adapter 必须表现为禁用。

## 10. 统一检索契约

### 10.1 检索实体

统一结果类型为：

```json
{
  "object": "creative_asset_match",
  "result_type": "image_asset | video_segment",
  "id": "asset-or-segment-id",
  "asset_id": "asset-id",
  "asset_source_id": "src-id",
  "start_ms": 12000,
  "end_ms": 17800,
  "thumbnail_url": "/v1/assets/.../thumbnail",
  "summary": "...",
  "matched_terms": ["..."],
  "score": 0.82,
  "confidence": 0.77,
  "analysis_run_id": "arun_...",
  "analysis_revision": 1,
  "score_components": {"lexical": 0.7, "semantic": null, "recency": 0.1},
  "source_availability": "available",
  "provenance": ["visual", "transcript"]
}
```

图片的 `start_ms/end_ms/analysis_run_id/analysis_revision` 为 `null`。`asset_source_id` 是本次返回中经路径验证的可用 source；不得返回绝对路径。返回的视频时间范围 **MUST** 可直接用于 timeline source range；不得只返回整段视频。无语义模型时 `semantic=null`、`confidence=null`，不得为 fallback 伪造置信度。

### 10.2 排序

MVP SHOULD 复用现有 lexical/semantic/rerank 结构，对图片 `combined_text` 和视频片段 `combined_text` 做统一候选集；结果合并 **MUST**：

- 支持 `types=image,video_segment` 过滤；
- 支持 duration、orientation、date、required/excluded terms；
- 对同一视频连续近重复片段进行抑制；
- 返回分数来源，禁止将 lexical fallback 宣称为语义检索；
- 查询触发的定向回看结果 MUST 绑定查询与分析 revision。

查询在单个 SQLite 读事务中捕获 current analysis heads，并返回 `search_revision` 与 `analysis_heads` 摘要。refinement 完成后产生新 `search_revision`；已保存的 brief/timeline 依然引用创建时的 segment/run，不随 current head 漂移。

## 11. API 契约

### 11.1 通用规则

- 所有新响应 **MUST** 包含 `object`、`schema_version: "1"` 和稳定 ID。
- 时间单位统一为整数毫秒；日期为 UTC ISO 8601。
- 创建资源、修订 revision 和启动 job 的写 API **MUST** 要求 `Idempotency-Key`；语义严格遵守第 9.6 节的 `request_sha256 + response snapshot`契约。取消本身幂等，可不要求 key。
- 列表 API **MUST** 使用 cursor 分页，不使用不稳定 offset。
- API 仍仅绑定 loopback；Electron renderer 使用现有桌面 session token。
- 路径输入只允许已批准 root 下的相对路径或 root ID；浏览器端不得提交任意绝对路径。

认证是写操作的必要条件，不是“请求来自本机”的推论：

- Electron renderer 只能用每次启动随机 desktop session token 调用其被授予的 UI 路由；注册 root/签发 export grant 必须由 Electron main 在原生选择器结果上执行。
- Codex 写入必须携带由桌面端确认后签发的独立、短期、resource-scoped capability；见第 14 节。
- 无 `Origin`、loopback source address、可访问公开读 API、桌面应用正在运行，以及 `MEMOLENS_PLUGIN_TRUST_LOCAL_API=1` **MUST NOT** 单独授权任何新写 API。
- capability 不匹配 scope/resource、过期、被撤销或重放时返回 `403 capability_denied`；未认证返回 `401 authentication_required`。

错误对象：

```json
{
  "object": "error",
  "schema_version": "1",
  "error": {
    "code": "invalid_timeline",
    "type": "invalid_request_error",
    "message": "Clip source_out_ms exceeds asset duration.",
    "field": "tracks[0].clips[2].source_out_ms",
    "retryable": false,
    "details": {"asset_duration_ms": 12000},
    "request_id": "req_..."
  }
}
```

错误 HTTP 映射：格式/字段 `400`，认证 `401`，授权/路径 `403`，不存在 `404`，revision/idempotency/覆盖冲突 `409`，payload 过大 `413`，能力缺失 `422`，预算超限 `429`，可重试本地繁忙 `503`。`message` 面向用户且不含绝对路径；机器逻辑只使用 `code`。

### 11.2 索引与任务

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/v1/assets/import` | 导入图片/视频引用并创建探测/索引任务 |
| `POST` | `/v1/index/jobs` | 对一个或多个 asset 启动/继续索引 |
| `GET` | `/v1/index/jobs/{job_id}` | 状态、stage、progress、partial errors |
| `POST` | `/v1/index/jobs/{job_id}/cancel` | 幂等取消 |
| `POST` | `/v1/index/jobs/{job_id}/resume` | 从持久化 checkpoint 恢复 |
| `GET` | `/v1/assets/{asset_id}` | 文件与分析状态 |
| `GET` | `/v1/video-segments/{segment_id}` | 片段、关键帧、重叠转写 |
| `POST` | `/v1/video-segments/{segment_id}/refine` | 有界定向回看 |

`POST /v1/assets/import` **MUST** 支持 `dry_run=true`，先返回将新增/跳过/拒绝的文件。

Import 请求仅接受 `{"library_root_id":"root_...","relative_paths":["a.mov"],"dry_run":false}`；目录扫描使用 `relative_paths:["."]` 与显式 `recursive`/`max_files`，不接受绝对路径。非 dry-run 成功返回 `202`、`assets[]`、`job_ids[]`、`skipped[]`与 `rejected[]`。`POST /v1/index/jobs` 请求为 `asset_ids[] + analysis_profile_id + transcription.mode(off|auto|local)`；`resume` 必须引用原 job/checkpoint，不新建 analysis revision。

统一 job 响应必须包含 `id/kind/status/stage/progress/attempt/cancel_requested/created_at/started_at/heartbeat_at/finished_at/error/partial_errors/resumable`，但不包含 DB locator、绝对路径或未脱敏 stderr。终态 `succeeded` 的 progress 必须为 1。现有 `POST /v1/indexing/jobs` 作为 v0.2.0 图片同步兼容路由至少保留一个发布周期；它不接收视频，新 UI 不得使用它启动长任务。

### 11.3 混合检索

`POST /v1/search/mixed`

```json
{
  "query": "雨夜里人物独自行走，但不要正脸",
  "types": ["image", "video_segment"],
  "top_k": 24,
  "filters": {
    "duration_ms": {"min": 1000, "max": 15000},
    "orientation": "portrait",
    "excluded_terms": ["正脸"]
  },
  "refinement": {"mode": "auto", "max_segments": 3, "budget_frames": 300}
}
```

若 refinement 仍在运行，响应 MAY 为 `202` 并返回 job；调用者可选择使用现有结果或等待精查。

`200` 响应包含 `query_id/search_revision/results[]/analysis_heads/next_cursor/refinement_jobs[]`。`202` 仍必须返回已有结果快照，并通过 `refinement_pending=true` 和 job ID 表明可稍后获取新 search revision。

### 11.4 Creative brief

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/v1/creative/briefs` | 将用户意图规范化并检索素材 |
| `GET` | `/v1/creative/projects/{project_id}` | 项目、brief、素材建议 |
| `POST` | `/v1/creative/projects/{project_id}/brief/revise` | 修订 brief；保留 revision |

Brief **MUST** 包含：goal、audience、platform、duration_ms、aspect_ratio、tone、pace、must_include、must_exclude、narrative_arc、candidate_refs、missing_assets、assumptions。生成 brief 可以使用模型，但服务端 **MUST** schema-validate，并确认所有 candidate refs 存在。

`candidate_refs[]` 必须固定 `asset_id/asset_source_id/segment_id?/source_in_ms?/source_out_ms?/analysis_run_id?/reason/evidence`；`missing_assets[]` 使用 `description/why_needed/required(false|true)/suggested_capture`，不能包含伪造 ID。Phase 1 无 LLM 时使用可版本化规则模板并标记 `generator=rules-v1`。

### 11.5 Timeline

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/v1/creative/projects/{project_id}/timelines` | 从 brief 创建 revision 1 |
| `POST` | `/v1/timelines/{id}/revise` | 以自然语言或 typed ops 创建新 revision |
| `POST` | `/v1/timelines/{id}/validate` | 纯校验，不渲染 |
| `GET` | `/v1/timelines/{id}?revision=N` | 获取不可变 revision |
| `GET` | `/v1/timelines/{id}/revisions` | revision 历史 |

自然语言 revise **MUST** 先转为 typed operations，例如 `replace_clip`、`trim_clip`、`move_clip`、`set_volume`、`set_text`、`set_format`。服务端应用操作并保存新 revision；禁止让模型直接覆盖 timeline JSON。

Create 请求必须指定 `brief_revision`；revise 必须指定 `base_revision` 以及 `instruction` 或 `operations[]`（二选一）。允许的 P0 typed ops 为 `add_clip/remove_clip/replace_clip/relink_source/trim_clip/move_clip/set_duration/set_crop/set_fit/set_volume/set_text/set_format/add_transition/remove_transition`；每个 op 必须包含 `op_id`、目标 ID 和 `preconditions`。自然语言转换后 API 先返回 typed diff 预览；只有 `apply=true` 才写新 revision。

### 11.6 Render

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/v1/renders` | 启动 `preview` 或 `export` |
| `GET` | `/v1/renders/{job_id}` | 状态、进度、错误和输出元数据 |
| `POST` | `/v1/renders/{job_id}/cancel` | 取消并清理未完成临时文件 |
| `GET` | `/v1/renders/{job_id}/download` | 下载已完成且校验过的结果 |

`download` **MUST** 只服务数据库记录的完成产物；不得接受调用方路径。未完成、失败或 hash 不匹配必须拒绝。

Render start 请求为 `timeline_id + timeline_revision + expected_timeline_sha256 + profile + output`。Preview 的 `output={"root_id":"<app-preview-root>"}`；export 的 `output={"export_grant_id":"grant_..."}`，客户端不另传路径或文件名。成功启动返回 `202 render.job`；状态仅在 `succeeded` 时包含 `download_url/output_sha256/duration_ms/size_bytes`。`download` 再次验证 job 的 output root/grant 绑定和实际 SHA，并使用 `Content-Disposition` 安全文件名。

## 12. Timeline JSON Schema

### 12.1 顶层

```json
{
  "schema_version": "1.0",
  "id": "tl_...",
  "project_id": "proj_...",
  "revision": 3,
  "format": {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "sample_rate": 48000,
    "duration_ms": 30000,
    "background_color": "#000000"
  },
  "tracks": [],
  "transitions": [],
  "provenance": {
    "created_by": "user | director | codex",
    "parent_revision": 2,
    "brief_revision": 1,
    "operations": [],
    "source_analysis_runs": {"seg_123": "arun_456"},
    "source_assets": {"asset_video_1": {"sha256": "...", "asset_source_id": "src_..."}},
    "created_at": "2026-08-12T00:00:00Z"
  }
}
```

Timeline Schema 1.0 **MUST** 以独立 JSON Schema 文件发布，所有 object 默认 `additionalProperties:false`。未知 schema version 必须拒绝，不做“尽力而为”渲染。`format.duration_ms` 必须等于所有 clip 组合区间的最大 end（空白区域渲染背景/静音）。JSON 内容 hash 使用第 9.6 节相同 canonical JSON 规则，但不包含 DB 外层存储字段。

### 12.2 轨道与 clip

轨道类型 **MUST** 支持 `video`、`image`、`text`、`audio`：

```json
{
  "id": "track_v1",
  "type": "video",
  "role": "primary",
  "z_index": 0,
  "muted": false,
  "clips": [
    {
      "id": "clip_1",
      "asset_id": "asset_video_1",
      "asset_source_id": "src_video_1",
      "segment_id": "seg_1",
      "source_in_ms": 13200,
      "source_out_ms": 16800,
      "timeline_start_ms": 0,
      "timeline_duration_ms": 3600,
      "fit": "cover",
      "crop": {"x": 0.1, "y": 0, "width": 0.8, "height": 1},
      "volume_db": -3,
      "audio_enabled": true,
      "provenance": {"reason": "开场建立环境", "match_id": "seg_1"}
    }
  ]
}
```

图片 clip 不含 source range，必须含 `timeline_duration_ms`；视频/音频 clip 必须含 source range，P0 不支持变速，因此 `timeline_duration_ms == source_out_ms - source_in_ms`。视频自带音频由 `audio_enabled/volume_db` 控制；独立音频 clip 另支持 `fade_in_ms/fade_out_ms`。文字 clip **MUST** 包含 `text/font_family/font_size/color/background/position/alignment/timeline_start_ms/timeline_duration_ms`；字体仅允许已打包或冻结系统 allowlist。字幕是 `text` track 的 `role=subtitle` 子类型，可附 `transcript_segment_ids[]`，但文本和时间在 timeline 中是可审计快照，不会随后续转写漂移。

P0 `fit` 仅支持 `contain/cover/stretch`（UI SHOULD 不推荐 `stretch`），裁剪坐标是 0–1 归一化值。轨道 `role` 为 `primary/overlay/subtitle/music/voiceover`；必须恰有一个 primary visual track，overlay 可依 z-index 重叠，音频轨的 z-index 无效且必须为 0。

转场不存在 clip 上，而存在顶层 `transitions[]`，避免两个 clip 对同一转场互相矛盾：

```json
{"id":"tr_1","type":"crossfade","from_clip_id":"clip_1","to_clip_id":"clip_2","duration_ms":250}
```

P0 仅支持 `crossfade` 和 `fade_to_black`；无转场就没有记录，不写 `none`。`crossfade` 要求同一 primary visual track 上相邻的 A/B 交叠时长恰等于 transition duration，即 `B.start = A.end - duration`；除此情况外 primary clip 不得重叠。`fade_to_black` 只需 `from_clip_id`、`to_clip_id=null`，在 from clip 尾部内执行，不改变时间区间。两者时长均为 `1..1000 ms`且不超过涉及 clip 的一半。视频 crossfade 对已启用的内嵌音频应用同时长等功率音频交叉淡化；独立音频轨仍按自身 fade 混音。

### 12.3 校验约束

Timeline validator **MUST** 检查：

- ID、schema version 与 revision 合法；
- 输出尺寸 240–3840、fps ∈ {24,25,30,50,60}、duration > 0；
- 所有 asset/segment 存在且属于已批准 root；
- 每个 source 存在、属于该 asset、状态可用、内容 SHA 与 provenance 一致；
- `source_in >= 0`、`source_out > source_in` 且不超过媒体 duration；
- segment 引用时必须与 asset 相同，source range 位于 segment 内（允许 1 帧的整数毫秒误差）；需要扩大范围时必须通过 typed op 去掉/替换 segment provenance，不存在“显式覆盖”后门；
- timeline start/duration 为非负整数且不超过项目最大时长（默认 30 分钟）；
- 主轨只有经合法 `crossfade` 描述的相邻 clip 可重叠；overlay 轨和 z-index 合法；
- 转场引用相邻 clip、交叠/时长精确符合上述语义，一个 clip boundary 最多一个 transition；
- crop 各值在 `[0,1]`、`width/height > 0`、`x+width <= 1`、`y+height <= 1`；
- 音量在 `[-60, 12] dB`；
- 文本长度、字体和颜色有效；
- 视频/音频无变速等式、文字/图片必填字段和 track/clip kind 一致；
- `format.duration_ms` 等于组合最大 end，并且输出帧量不超过上限；
- provenance 引用存在。

毫秒到输出帧的规范化 **MUST** 固定为非负值 half-up：`frame = floor(ms * fps / 1000 + 0.5)`，clip 的帧时长以规范化后的绝对 end-start 计算。渲染 plan 存储规范化帧号；不允许各处各自四舍五入。失败 **MUST** 返回稳定 `code + JSON Pointer` 的逐字段错误；validator 是纯函数，不得自动修改用户内容。单独的 `normalize` typed operation MAY 提供显式修复建议。

## 13. FFmpeg 渲染

### 13.1 确定性

- 渲染器 **MUST** 只消费已验证 timeline revision。
- FFmpeg 参数 **MUST** 由只接受已知 enum/数值/验证后文本的 typed builder 生成数组，以 `shell=False`、`-nostdin`、独立进程组运行，并保存脱敏后的 `ffmpeg_command_json`。用户文本不得成为 filter 表达式；过长 filter graph 写入 job 目录中的已转义 script。
- FFmpeg 输入仅允许已验证的本地普通文件，显式禁止 `http/https/tcp/udp/concat/crypto` 等非必要协议；不允许用户提供 concat list 或 filter script。渲染前必须逐 source 执行 root、source ID 和 SHA 重验。
- 相同素材 hash、timeline content hash、字体 hash、FFmpeg/ffprobe 构建标识、平台/编码器与 render profile SHOULD 产生同一 render cache key。“确定性”指同一冻结渲染环境中的可重放 plan 和时间语义，不承诺跨 FFmpeg/硬件编码器产生 bit-identical MP4。
- 字体、色彩空间、分辨率、fps 和编码参数 **MUST** 固定在 profile。
- 产物先写同目录临时文件，成功且 ffprobe 校验后原子 rename。
- 旋转必须通过 `-noautorotate` 加基于 ffprobe snapshot 的显式转换实现；缩放/pad/crop、像素格式、色彩范围和音频 sample rate 必须出现在可快照的 render plan。

### 13.2 Profile

P0：

- `preview-low`：最长边 720、H.264 fast、AAC 128k；
- `export-1080p`：timeline 尺寸（最大 1080p）、H.264 medium、AAC 192k、`+faststart`。

P0 软件 profile 使用 `libx264`（`yuv420p`）和 AAC；Setup **MUST** 通过实际 1 s 合成 encode/decode probe 验证能力，不仅查名称。缺少 `libx264` 时返回 `encoder_unavailable`，不静默改用硬件 encoder。发布必须附 FFmpeg 来源/构建配置和相关 license notice，但本 Spec 不做法律兼容性结果声明。

P1 MAY 增加 4K，但未通过性能与磁盘预算前不得开放。

### 13.3 失败处理

- 保留 stderr 最后 64 KiB，路径和 token 必须脱敏；
- 中间文件放入 job 专属临时目录；取消/失败后清理，诊断 manifest 可保留；
- 磁盘不足在开始前预检，运行时仍失败则返回 `insufficient_storage`；
- FFmpeg 缺失、编码器缺失、损坏输入、字体缺失、时间线无效使用不同错误码；
- 用户可对相同 revision 重试；不得悄悄改用不同 timeline。
- export grant 失效、被消耗、目标已存在、output root 变化、source 变化与 DB 绑定不符必须分别使用 `export_grant_invalid`、`output_exists`、`output_root_changed`、`source_changed`、`database_binding_mismatch`；不得统一包装成 `ffmpeg_failed`。

## 14. Codex 插件与 Skill

现有仓库插件与 `use-memolens` Skill **MUST** 扩展，而不是创建第二套不一致集成。

### 14.1 安全默认

- 插件默认仍为离线、无第三方 API key、无 HTTP/DNS。
- safe-default 只读 SQLite 模式 **MUST** 增加图片 + 视频片段混合检索、项目/timeline 读取和 timeline 离线校验。
- 插件不得扫描未配置路径、发现桌面 token 或绕过 capability。
- Codex 对少量关键帧的本地查看受当前工作区数据控制约束；Skill **MUST** 提醒用户这是当前 Codex 会话内的图像输入。

### 14.2 写操作与确认点

以下操作视为写操作：创建项目、创建/修订 timeline、启动预览、启动导出、取消任务。它们 **MUST** 通过受认证桌面桥接或用户显式 opt-in 的受限本地 API 完成；不得复用当前“未认证 originless loopback”作为默认写通道。

这里的授权必须是桌面端在用户确认后签发的**独立短期 scoped capability**，而不是环境开关。capability **MUST**：

- 使用至少 256 bit 随机 bearer secret，服务端仅保存 hash；
- 绑定 `database_uuid`、主体、允许的 action、project/timeline/job resource IDs 和允许的 output/export grant；
- 默认单次使用，最长 TTL 10 分钟；使用、过期、撤销后 fail closed；
- 对每次写请求校验 capability scope，并把 capability ID 纳入幂等 `scope`；
- 由 Electron main 经原生确认 UI 签发，Codex/插件无权自行签发、续期或扩大 scope；
- 不得写入日志、SQLite 响应快照或暴露给 renderer 之外的调用方。

`Origin` 缺失、请求来自 loopback、本机用户可访问 API、已有 desktop token、`MEMOLENS_PLUGIN_TRUST_LOCAL_API=1`、聊天中的用户同意，以及机器上存在任何 provider API key，单独或组合起来都 **MUST NOT** 被视为该 capability。桌面 token 也不能替代 Codex scoped capability。

实现 **MUST** 在插件状态中报告：

```json
{
  "mode": "safe_default_read_only | authenticated_desktop_write",
  "capabilities": {
    "search_assets": true,
    "read_timeline": true,
    "validate_timeline": true,
    "create_timeline": false,
    "render_preview": false,
    "export_video": false
  }
}
```

确认策略：

- 纯检索、读取、草拟但不保存：无需确认；
- 保存新项目/timeline revision：UI 或 Codex 工具调用前必须显示目标项目与变更摘要；
- 渲染预览：确认输出在 app-managed preview 目录，可一次授权当前项目；
- 最终导出：每次必须明确输出目录、文件名、预计覆盖行为；默认禁止覆盖；
- 删除原始素材、移动素材、任意路径写入：MVP 不暴露。

### 14.3 建议工具

Skill/MCP SHOULD 暴露：

- `memolens_status`
- `memolens_search_assets`
- `memolens_get_segment`
- `memolens_create_brief`
- `memolens_get_timeline`
- `memolens_validate_timeline`
- `memolens_create_timeline`（仅写模式）
- `memolens_revise_timeline`（仅写模式）
- `memolens_render_preview`（仅写模式）
- `memolens_export_video`（仅写模式）
- `memolens_job_status` / `memolens_cancel_job`

工具返回 **MUST** 使用稳定 ID 和相对引用；不得向模型返回不必要的绝对路径。CLI fallback 保持 Python 标准库。

## 15. 隐私与数据出境

### 15.1 本地默认

默认模式：

- ffprobe、FFmpeg、分镜、抽帧、去重、渲染、SQLite、lexical fallback 全部本地；
- 无本地转写/VLM 时功能降级而不是联网；
- 应用启动、索引、检索、渲染不得产生 DNS/HTTP；
- UI **MUST** 显示“本地模式”及当前不可用的语义能力。

### 15.2 外部能力 opt-in

外部 VLM/转写 **MUST** 在 Setup 中按能力分别 opt-in，不能用一个模糊“启用 AI”同时授权原视频和音频出境。确认页必须说明：

- provider 和 model；
- 发送原始视频、音频、代表帧、缩略图、转写文本还是 metadata；
- 时间范围与预计数量；
- provider retention 假设或“未知”；
- 日志内容与撤回方式。

即使环境变量、`.env`、系统钥匙串或既有图片 profile 中已经存在可用的外部模型 API key，视频代表帧、原始帧、音频和转写文本也 **MUST NOT** 自动外发。每个外部视频分析/转写 job 必须持有第 9.6 节的独立 `provider_opt_in_grant`；仅有 key 时 adapter 状态必须是 `configured_but_not_authorized`。图片分析的历史授权不得继承为视频或音频授权，某个 provider/capability 的授权也不得扩张到另一 payload class。

默认策略 SHOULD 是只发送压缩代表帧；原始视频/完整音频出境必须二次显式授权。每次外部 job **MUST** 写 `provider_payload_manifests`，即使调用失败也保留尝试记录。

### 15.3 日志与保留

- 日志 **MUST** 脱敏绝对路径、API key、桌面 token、用户对白全文和 provider 请求体。
- 普通日志可记录 asset/segment ID、时间范围、字节数、provider/model、状态和错误类型。
- 关键帧缓存默认保留至用户重建索引或清缓存；预览临时文件默认 7 天；导出文件由用户管理。
- payload manifest 默认长期保留，用户可导出或清除审计记录；清除行为必须独立于原始素材。
- 外部 provider 的实际保留政策 **MUST NOT** 被 MemoLens 宣称为“零保留”，除非对应配置和证据被冻结。

### 15.4 Offline network-deny

CI/验收 **MUST** 包含阻断网络的测试：

- monkeypatch/socket deny 或隔离网络环境；
- 本地导入、探测、粗扫描、fallback 索引、混合 lexical 检索、timeline validate、预览渲染均通过；
- 任何静默外部 fallback 使测试失败；
- 保存 `offline_network_deny.log` 作为 Spec 004 工件的一部分。

## 16. UX 状态机

### 16.1 Setup

状态：

```text
needs_library
→ probing_ffmpeg
→ ready_local
→ optional_ai_setup
→ ready_enhanced
```

MUST 显示：素材根目录、输出目录、FFmpeg状态、可用转写/VLM能力、数据出境模式。FFmpeg 缺失时提供可执行诊断，不提供假数据演示替代真实能力。

### 16.2 Library

用户可导入图片/视频、查看索引状态、过滤 asset 类型，并展开视频的片段时间轴。状态包括：

- 空：解释支持格式和“导入素材”；
- 加载：显示当前 stage 和可取消进度；
- 部分：图片/视频可用，但转写或语义分析不可用；
- 失败：逐文件错误及重试；
- 取消：保留已完成片段并标记不完整；
- 恢复：应用重启后继续安全步骤。

### 16.3 Create

表单与对话共同编辑 brief。MUST 提供目标、时长、画幅、平台、情绪、节奏和排除条件的显式控件；模型建议不得隐藏这些约束。候选素材展示具体视频时间段和选用理由。

### 16.4 Storyboard / Timeline

- Storyboard 卡片：缩略图、来源、source range、timeline range、理由、替换入口；
- Timeline：P0 提供简化多轨视图，不追求专业 NLE 手势；
- 所有自然语言修改显示 operation diff，再保存 revision；
- 验证错误可定位到 clip；
- 撤销通过切回上一个 revision，而非逆向猜测。

### 16.5 Preview / Export

- 预览前显示 timeline validation；
- 渲染中显示百分比、阶段、取消；
- 失败显示可操作错误，不暴露完整命令/路径；
- 完成显示播放、在 Finder 中显示、重新导出；
- 覆盖现有文件默认禁止，必须使用新名字或明确确认。

### 16.6 响应式布局

桌面目标：1280×800 及以上完整 timeline。390px 移动布局 **MUST**：

- 无页面级横向溢出；
- Setup/Library/Create 可完整使用；
- Storyboard 纵向卡片可编辑；
- Timeline 降级为轨道列表和 clip inspector，不强行展示桌面密集时间尺；
- Preview/Export 可查看状态与取消；
- 触摸目标至少 44×44 CSS px。

## 17. 测试策略

### 17.1 合成 fixture

仓库 **MUST** 通过脚本确定性生成小型媒体 fixture，不提交私人素材：

- 20–30 秒、30 fps、H.264/AAC MP4；
- 至少 4 个明显镜头：纯色标题、移动方块、人物替代图形、PPT式文字；
- 包含 0.5 秒短镜头、黑场、静音段、测试音和内嵌时间码；
- 一个无音轨视频；一个旋转 metadata MOV；一个损坏/截断文件；
- 3 张图片和 1 个独立音频；
- 固定生成 seed、FFmpeg 命令和 SHA manifest。

### 17.2 单元测试（P0）

- ffprobe JSON 解析、rotation/fps/duration；
- 路径 traversal、symlink escape、扩展名欺骗；
- shot boundary、代表帧选择、fallback interval；
- 时间重叠关联 transcript；
- migration v1→v2、重复执行、checksum mismatch、回滚；
- timeline schema 与所有边界条件；
- typed revision operations 和 provenance；
- FFmpeg command builder 参数数组；
- error object、idempotency key 冲突；
- 日志脱敏与 payload manifest。

### 17.3 集成/API 测试（P0）

- 导入 fixture → job complete → segments/keyframes 可查；
- 无转写 provider 时 partial success；
- 图片 + video segment 混合检索；
- brief 引用全部存在；
- timeline create/revise/validate；
- render preview/export/status/download；
- 索引和渲染取消；
- 进程重启后 interrupted/resume；
- 同一 idempotency key 不重复创建；
- 未认证/非 loopback/不可信 Origin 被拒绝；
- offline network-deny 全链路。

### 17.4 FFmpeg golden test（P0）

Golden 不应逐字比较有版本差异的压缩文件 hash。必须比较：

- ffprobe：duration 容差 ±1 frame、尺寸、fps、codec、audio stream；
- 预定义时间点截图的感知/像素差阈值；
- 音频起止、静音区间和峰值范围；
- 文字 overlay 在预期帧可见；
- 转场前中后代表帧符合阈值；
- 同 profile 的 command plan snapshot 稳定。

### 17.5 Electron 与浏览器验收（P0）

真实 Electron 测试 MUST 覆盖：Setup → 选目录 → 导入 → 索引 → Create → Timeline → Preview → Export。浏览器视觉 QA MUST 保存：

- 1440×1000：Library、Create、Timeline、Preview；
- 390×844：Setup、Library、Storyboard、render state；
- 空、加载、partial、failed、cancelled、recovered 状态至少各一张；
- 控制台无 error、图片无 broken、页面无横向溢出。

### 17.6 Codex 插件测试（P0）

- safe-default 测试证明无 DNS/HTTP；
- 只读 SQLite 可搜索图片和视频片段；
- traversal result 被拒绝；
- 无写 capability 时创建 timeline/render 明确失败；
- authenticated write 模式下 create/revise/preview/export 测试使用隔离临时目录；
- 插件不得读取桌面认证文件；
- 所有工具 schema、CLI fallback 和 marketplace manifest 校验通过。

### 17.7 P1

- Windows 11 Electron 与 FFmpeg；
- HEVC/旋转/可变帧率更多样本；
- 大视频取消与磁盘耗尽；
- 外部 VLM/转写的契约录制测试（不在公共 CI 发送真实媒体）；
- accessibility keyboard/focus audit；
- 1k+ 混合资产规模 benchmark。

## 18. 性能预算

预算在基准机（Apple Silicon、16 GB RAM、SSD，记录具体型号）上测量。未冻结基准前只作为工程门槛，不得作为营销结果。

P0 目标：

| 操作 | 预算 |
|---|---|
| ffprobe 单文件 | p95 < 1 s |
| Pass A 20–30 s fixture | < 10 s，峰值 RSS < 800 MB |
| 1 小时 1080p 本地粗扫描 | SHOULD ≤ 0.5× 实时；MUST 不超过 2× 实时作为发布门槛 |
| 默认代表帧数量 | SHOULD ≤ 1500 / 小时；硬上限 3600 / 小时 |
| SQLite 混合 lexical 查询（10k 单元） | p95 < 500 ms |
| Timeline validate（200 clips） | p95 < 200 ms |
| 30 s 720p preview | SHOULD ≤ 1× 实时；MUST ≤ 3× 实时 |
| 取消响应 | UI < 250 ms；子进程终止 p95 < 3 s |
| UI progress 更新 | 至少每 1 s；不得阻塞 renderer |

索引速率受设备、codec 和模型影响，发布说明不得给出未经冻结 benchmark 的普遍成本或速度数字。

## 19. P0/P1 发布门槛

### 19.1 P0：阻断发布

- [ ] 现有图片 `image_index` 数据无损迁移，旧图片检索回归通过。
- [ ] 所有连接回读 `foreign_keys=1`、`journal_mode=wal`，并验证统一 `busy_timeout`；并发与锁冲突测试通过。
- [ ] 同内容多路径只产生一个 asset、多个 asset source；missing/changed/relink 不改写内容身份。
- [ ] 默认查询只读 current successful analysis head；失败/取消的高 revision 不遮蔽上一个成功 revision。
- [ ] 图片与 P0 视频格式均可导入，ffprobe 能力错误可解释。
- [ ] 自适应 Pass A/B 真实运行，关键帧带时间戳和选择理由。
- [ ] 无转写、转写失败均能形成可用 partial index。
- [ ] 视频片段与图片可通过同一 API/界面检索。
- [ ] 至少一个查询触发有界定向回看并生成新 analysis revision。
- [ ] creative brief 引用真实素材，缺失素材不伪造。
- [ ] timeline revision 1 可生成；自然语言修订产生 typed diff 和 revision 2。
- [ ] timeline validator 拒绝越界 source、非法 crop、未知 asset 和路径逃逸。
- [ ] 本地 FFmpeg preview 与 MP4 export 通过 golden test。
- [ ] 索引与渲染可取消；应用重启能显示 interrupted 并恢复/重试。
- [ ] 输出仅进入批准目录；不覆盖原始素材；下载接口不能任意读文件。
- [ ] library root、output root、export grant、source relink 和 symlink/path escape 边界全部通过。
- [ ] timeline/render 组合外键有效；切换当前 DB 后运行 job 仍固定在创建时 `database_uuid`，不可达时阻塞而不串库。
- [ ] 幂等记录保存 canonical `request_sha256` 与 response snapshot；同 key 异 payload稳定返回 409。
- [ ] 默认离线模式在 network-deny 下通过完整 MVP 闭环。
- [ ] 外部 provider 必须 opt-in，且每次调用产生 payload manifest。
- [ ] Codex safe-default 无 API key、离线只读；写工具仅在认证写模式可用。
- [ ] Codex 每个写操作都要求独立短期 scoped capability；originless/loopback/`TRUST_LOCAL_API`/desktop token 均不能替代。
- [ ] 环境已有外部 provider key 时，未单独 opt-in 的视频帧/音频仍零外发并显示 `configured_but_not_authorized`。
- [ ] Electron 端到端、插件、API、单元、FFmpeg golden 全绿。
- [ ] 1440px 与 390px 真实截图验收，无横向溢出和控制台错误。
- [ ] README/CHANGELOG/隐私说明与实际能力一致；不可声称项已检查。

### 19.2 P1：不阻断首个 MVP，但必须登记

- [ ] Windows 11 打包与端到端验收。
- [ ] 词级转写与 speaker diarization。
- [ ] 更强 OCR/本地视觉 embedding 和镜头聚类。
- [ ] 多 overlay 视频轨、更丰富转场和音频 ducking。
- [ ] 查询 refinement 的预算可视化与手动控制。
- [ ] 4K 输出、代理媒体和后台缓存清理策略。
- [ ] 与专业 NLE 的单向交换格式导出。

## 20. 分阶段实施计划

### Phase 1：端到端闭环（本轮 MUST）

该阶段不得只交付 UI：

1. schema v2 幂等迁移与 `assets`/segments/keyframes/transcripts/projects/timelines/jobs 表；
2. FFmpeg/ffprobe 能力探测、路径边界和媒体导入；
3. 本地 Pass A + 代表帧 Pass B；无 VLM/转写 fallback；
4. 片段级结构化索引和图片/视频混合 lexical 检索；
5. brief 创建、确定性 timeline schema、typed revise、validator；
6. FFmpeg preview/export、job cancel/recovery；
7. Setup/Library/Create/Storyboard-Timeline/Preview-Export 状态机；
8. Codex 插件只读检索与认证写路径的最小工具；
9. 合成 fixture、P0 自动化测试与真实截图。

Phase 1 可选择规则/模板 Director，不要求外部 LLM。若没有模型，系统 MUST 仍能根据用户显式字段和检索结果生成确定性 brief/timeline 草案。

### Phase 2：增强理解

- 可选本地/外部转写 adapters；
- VLM 结构化片段描述；
- OCR/音频事件；
- 查询驱动 refinement 排序；
- payload manifest UI；
- 更强故事板解释和缺失素材建议。

### Phase 3：编辑能力扩展

- 音频 ducking、节拍辅助切点、更多受限转场；
- 代理媒体、4K、更多输入格式；
- Windows 正式支持；
- 单向 NLE interchange；
- benchmark 驱动的采样/检索调优。

以下继续推迟：人体姿态、目标跟踪、身份重识别、复杂多轨专业 NLE、自动配乐版权库、生成式视频补镜头。

## 21. 与 Spec 004 的关系与声明治理

本 Spec 证明的是“视频创作闭环可以被实现和运行”，不自动证明效果、规模、成本或隐私结果。

Spec 004 当前仍为：`PROPOSED / 未实施 / 简历资格 NO`。因此：

- 可以表述：实现了本地媒体探测、自适应抽帧、片段索引、混合检索、结构化 timeline 和 FFmpeg 导出；前提是 P0 测试真实通过。
- 不得表述：检索准确率提升、关键事件召回率、处理 1k/5k/10k 素材的性能、每小时固定成本、零数据出境、零云依赖、无遗漏视频理解，除非冻结 benchmark 产出证据。
- 视频扩展后，Spec 004 benchmark SHOULD 新增 `video_segment` query、时间范围 relevance、短事件召回、代表帧覆盖、refinement 增益和每小时分析成本，但不得事后修改预注册阈值。
- 所有效果数字必须绑定：dataset manifest、query labels、index snapshot、model/prompt/provider、analysis profile、FFmpeg version、payload manifest 和 raw result。
- 隐私声明必须由 offline network-deny log 和 provider payload manifest 支撑；“本地默认”是设计属性，“无数据出境”是需要运行证据的结果属性。

发布前 MUST 更新 claim ledger，逐条标记：`implemented`、`tested`、`benchmarked`、`publishable`。只有 `benchmarked=true` 且证据冻结的结果型表述可进入简历、官网或 Release。

## 22. 发布工件

P0 发布候选必须包含：

- schema migration 与 rollback/recovery 说明；
- Timeline JSON Schema 1.0；
- API OpenAPI/契约示例；
- 合成媒体 fixture 生成脚本与 manifest；
- P0 测试报告；
- FFmpeg golden 报告；
- Electron 桌面/390px 截图；
- offline network-deny log；
- provider payload manifest 示例（仅合成素材）；
- 支持格式/平台矩阵；
- 已知限制与不可声称项；
- 更新后的 Codex Skill、插件测试与 capability 输出；
- CHANGELOG 和升级说明。

## 23. 最终验收场景

发布负责人必须在隔离 app state 与合成 fixture 上完成：

1. 启动应用，确认默认无第三方 key、网络被禁用。
2. 选择素材库与输出目录，FFmpeg 检测通过。
3. 导入 3 张图片、带音频 MP4、无音频视频和旋转 MOV。
4. 取消一个运行中的索引任务，再恢复；已有结果不重复。
5. 搜索“蓝色标题后快速移动的物体”，同时得到图片和具体视频时间段。
6. 创建“15 秒竖屏、前静后快、不要黑场”的 brief。
7. 生成 timeline revision 1，所有引用真实存在。
8. 通过对话执行“第二个镜头缩短一秒，把最后一张图延长两秒”，生成 typed diff 和 revision 2。
9. 注入越界 source 路径，validator 精确拒绝；原 timeline 不被修改。
10. 渲染 720p preview，取消一次后重试成功。
11. 导出 H.264/AAC MP4，ffprobe 与 golden 断言通过。
12. 使用 Codex safe-default 搜索并读取 timeline；写操作被拒绝。
13. 启用隔离的认证写模式，由 Codex 创建 revision 3 并请求预览；输出仍限制在批准目录。
14. 检查日志不含完整路径、token、对白全文；offline run 无网络访问。
15. 在 1440×1000 与 390×844 完成真实 UI 截图验收。

以上 15 项全部通过，且第 19.1 节所有 P0 checkbox 有对应测试或人工证据后，Video Creative Workbench 才可标记为 release candidate。

## 24. 设计依据与非功能承诺

以下研究只解释本 Spec 的设计取舍，**不增加 Phase 1 实现范围，也不构成 MemoLens 的效果、成本或准确率承诺**：

- CVPR 2025 的 [T*](https://openaccess.thecvf.com/content/CVPR2025/html/Ye_Re-thinking_Temporal_Search_for_Long-Form_Video_Understanding_CVPR_2025_paper.html) 将长视频理解表述为从大量帧中寻找少量查询相关帧，并使用时间与空间上的自适应 zoom-in；其 LV-Haystack 实验中，现有搜索方法在一个子集上的 temporal F1 仅为 2.1%。这支持第 7 节“先粗搜候选、再按查询回看”，也说明一次性关键帧选择 **MUST NOT** 被宣称为完整视频理解。
- CVPR 2025 的 [DeCafNet](https://openaccess.thecvf.com/content/CVPR2025/html/Lu_DeCafNet_Delegate_and_Conquer_for_Efficient_Temporal_Grounding_in_Long_CVPR_2025_paper.html) 使用廉价 sidekick 对全部 clips 做密集扫描，再把显著 clips 交给 expert encoder。该 delegate-and-conquer 结构支持 MemoLens 将本地 Pass A 与成本更高的 Pass B/C 分开，但论文报告的计算节省 **MUST NOT** 直接外推为 MemoLens 的性能数字。
- CVPR 2025 的 [Seq2Time](https://openaccess.thecvf.com/content/CVPR2025/html/Deng_Seq2Time_Sequential_Knowledge_Transfer_for_Video_LLM_Temporal_Grounding_CVPR_2025_paper.html) 强调统一、明确的时间表示对于 temporal grounding 的重要性。这支持本 Spec 使用整数毫秒 `start_ms/end_ms`、不可变 analysis revision 和精确 source range，而不是仅保存无边界的自然语言摘要。
- 2026 年的 [VideoBrain](https://arxiv.org/abs/2602.04094) 结合全视频语义检索与候选区间内的密集时间采样，并报告在其四个 benchmark 上减少 30–40% 帧使用量的同时提升基线结果。这进一步支持“semantic retrieval → interval dense sampling”的定向回看设计；该百分比只属于论文实验，MemoLens 在完成 Spec 004 冻结 benchmark 前 **MUST NOT** 引用为自身节省比例。

因此，本 Spec 的非功能承诺保持不变：系统 **MUST** 暴露采样 profile、analysis revision、时间范围、置信度与 refinement provenance；系统 **MUST NOT** 声称固定采样、单次代表帧或一次模型调用能够无遗漏理解长视频。任何“召回率、少看多少帧、节省多少成本、时间定位精度”的产品表述，仍须通过第 21 节规定的冻结 benchmark 与 claim ledger。
