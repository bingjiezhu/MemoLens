<p align="right"><a href="README.md">English</a> · <strong>简体中文</strong></p>

# MemoLens

**创作者的私人素材家 —— 本地记住，等你下次要发内容时再用。**

把素材放在你本来就在用的文件夹。MemoLens 在本机给 **照片** 建索引，视频从 **Create → 视频初剪** 导入。Inbox 只做轻量过片，不把回忆变成整理任务；真正要创作时，再从有来源依据的片段里找出可用画面。原片始终留在磁盘上。预览导出是有边界的 **720p 另存为**，不会覆盖源文件。

**许可。** 源码公开（source-available）双许可：[非商业 PolyForm Noncommercial 1.0.0](LICENSE) · [商业使用需单独授权](COMMERCIAL-LICENSE.md)。

<p align="center">
  <a href="https://github.com/bingjiezhu/MemoLens/releases/download/promo/memolens-promo.mp4">
    <img src="docs/assets/memolens-promo-poster.jpg" alt="观看 50 秒 MemoLens 流程：本地记住、Inbox 过片、找到瞬间、做出初剪" width="100%" />
  </a>
</p>

<p align="center"><sub>50 秒演示（英文字幕、纯器乐、无旁白）。<a href="https://github.com/bingjiezhu/MemoLens/releases/download/promo/memolens-promo.mp4">播放 MP4</a> · <a href="docs/assets/memolens-promo.mp4">下载</a></sub></p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#你能用到什么">产品</a> ·
  <a href="#架构">架构</a> ·
  <a href="#隐私">隐私</a> ·
  <a href="#许可">许可</a> ·
  <a href="CHANGELOG.md">更新日志</a>
</p>

<p align="center">
  <img src="docs/assets/memolens-home-v050.jpg" alt="MemoLens 0.5 首页：Inbox 与 Creator Memory 摘要" width="100%" />
</p>

<p align="center">
  <img src="docs/assets/memolens-inbox-v050.jpg" alt="MemoLens 0.5 媒体 Inbox，照片与视频可逆审阅" width="72%" />
  <img src="docs/assets/memolens-mobile-v050.jpg" alt="MemoLens 0.5 窄屏首页" width="22%" />
</p>

<p align="center">
  <img src="docs/assets/memolens-create-v050.jpg" alt="MemoLens 0.5 照片创作工作区" width="100%" />
</p>

<p align="center">
  <img src="docs/assets/memolens-video-v050.jpg" alt="MemoLens 0.5 视频初剪工作区" width="100%" />
</p>

---

## 你能用到什么

四个房间、一条本地闭环：**Home → Library → Memories → Create**。

| 房间 | 做什么 |
| --- | --- |
| **Library** | 选择你已经在用的文件夹。在这里建立 **照片** 索引。用 **Inbox** 审阅新照片和视频（保留、从 MemoLens 归档、收藏、标记就绪、撤销）。确认一小份 **Creator Memory** 创作档案 —— 未确认的推测不会变成默认偏好。 |
| **Memories** | 从同一份 SQLite 索引里重访主题、关键词星系、重复组和收藏篮。 |
| **Create** | 照片故事，或 **视频初剪**：想法 → 素材 → 简报 → 时间线 → 720p 预览 → 另存为。**视频从这里导入**，不是 Library 的照片索引顺带扫进去的。 |
| **Home** | Inbox 与 Creator Memory 的摘要，让下一步动作一眼能看清。 |

另外还包括：

- 自然语言检索（含排除词）、质量感知排序、近重复抑制
- 可逆的硬切时间线编辑（重排、替换、裁切、裁剪、适配）
- 本地 720p H.264/AAC 预览，再通过 Electron 另存为；已存在的目标文件不会被覆盖
- 可选视觉 / 查询模型档（MiniMax、Vertex/Gemini、OpenAI 兼容、DashScope、Ollama）。没有 API key 也能跑：元数据与 semantic-hash 回退仍然可用
- 可选 [Photon](photon-bot/README.md) Discord 桥，走同一套本地 API（**不是**应用内聊天）

**当前边界（如实）：** 视频检索是确定性元数据加可选 sidecar 文本，不是完整语义视频理解。1080p 成片导出仍关闭。逆地理编码默认关闭。

---

## 快速开始

**环境：** 桌面应用建议 macOS · Python 3.10+（推荐 3.11）· Node.js 22.12+ · FFmpeg/ffprobe 6+

```bash
git clone https://github.com/bingjiezhu/MemoLens.git
cd MemoLens
cp .env.example .env          # 可选：填服务商 key 或改用 Ollama
npm run setup:mac             # 虚拟环境、Node 依赖；缺 FFmpeg 时用 Homebrew 安装
./Launch\ MemoLens.command
```

完成 setup 后也可以 `npm run electron`。

**第一次打开应用**

1. **Library** —— 选中素材文件夹，建立 **照片** 索引。
2. **Create → 视频初剪** —— 导入 MP4/MOV/M4V，让它们进入同一资料库，再在 **Inbox** 里一起审阅。
3. 只把你真正想复用的偏好写入 **Creator Memory**。
4. 用 **Memories** 重访主题，或用 **Create** 做照片故事 / 视频初剪。

桌面状态在 `~/Library/Application Support/MemoLens`。私人素材库请放在 git 仓库外面。

**不想用私人素材时，先生成演示库**

```bash
npm run demo:library          # 12 张图 + 2 段视频；已被 gitignore
```

然后在应用里选择 `./demo-photo-library`。

**浏览器备用路径**（同一套 API，没有系统文件夹选择器）：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && npm install
python3 backend/app.py        # http://127.0.0.1:5519
npm run dev                   # http://127.0.0.1:5173
```

在 **Library → Advanced settings** 里填写照片库和 SQLite 路径。照片仍从 Library 索引；视频仍从 **Create → 视频初剪** 进入。一条命令拉起本地栈：`npm run dev:local`。

**开发：** `npm test` · `npm run verify:local` · [CONTRIBUTING.md](CONTRIBUTING.md)

---

## 模型档

`config.yaml` 把 **视觉**（照片索引）和 **查询/文案** 分开。默认：`minimax_vl01` / `minimax_m27` / 向量 `semantic_hash`（不需要本地 torch）。

```bash
export MINIMAX_KEY=...

export VISION_VLM_PROFILE=vertex_gemini25_flash
export QUERY_VLM_PROFILE=vertex_gemini25_flash
export VERTEX_PROJECT="your-gcp-project"

export VISION_VLM_PROFILE=ollama_gemma4_e4b
export QUERY_VLM_PROFILE=ollama_gemma4_e4b
```

未设置 `VERTEX_ACCESS_TOKEN` 时，后端会依次尝试 `gcloud` application-default 和 `gcloud auth print-access-token`。可选 CLIP/DINO：`pip install -r requirements-local-models.txt`。

不用桌面选择器时：

```bash
export IMAGE_LIBRARY_DIR="/absolute/path/to/your/photos"
export SQLITE_DB_PATH="$IMAGE_LIBRARY_DIR/photo_index.db"
```

已有索引可用 `npm run quality:backfill -- --force` 补离线 `aesthetic_score`，不必重跑视觉。

---

## 架构

<p align="center">
  <img src="docs/assets/memolens-workspaces.png" alt="MemoLens 0.5 四个房间：Home、Library、Memories、Create" width="100%" />
</p>

<p align="center">
  <img src="docs/assets/memolens-architecture.png" alt="MemoLens 架构：从界面到 SQLite 的本地分层" width="100%" />
</p>

<p align="center"><sub>可编辑画板：<code>docs/assets/memolens-architecture.html</code></sub></p>

```text
记住  →  审阅  →  查找  →  导演  →  剪辑  →  预览
                              ↑
             鉴权后的 Flask :5519（仅 loopback）
                              ↑
                    Electron  ·  浏览器
```

| 层 | 位置 | 职责 |
| --- | --- | --- |
| 界面 | `src/` | Home、Library、Memories、Create |
| 桌面 | `electron/` | 文件夹 / 另存为选择器、Application Support 里的 SQLite、Flask 监管、IPC |
| API | `backend/` | 本机 HTTP；照片索引与视频导入是不同路由 |
| 智能 | `indexing/`、`backend/src/retrieval/`、`backend/src/media/`、`core/` | 照片视觉、混合检索、Inbox、导演、时间线、720p 渲染 |
| 数据 | `core/db.py`、`core/media_db.py` | 图片索引 + 媒体 schema v3；原片永不覆盖 |

照片索引：`POST /v1/indexing/jobs`。视频导入：**Create → 视频初剪** → `POST /v1/assets/import`。React 界面在仓库根目录 `src/`；`frontend/` 只是遗留 Python 兼容导入，不是 UI。可选只读 MCP 插件在 `.agents/`（桌面应用不依赖它）。

```text
backend/     Flask API          electron/    桌面壳
core/        SQLite + 配置      src/         Vite + React 界面
indexing/    照片管线           photon-bot/  Discord 桥
scripts/     安装与校验         docs/        规格与演示片
```

设计记录：[Creator Memory 规格](docs/specs/006-creator-memory-media-inbox.md)（0.5.0 已交付）· [视频规格](docs/specs/005-video-creative-workbench.md)（0.3.0 已交付；文首仍保留提案期记录）· [产品策略](docs/product-strategy.md)。005 / 006 规格正文为中文。

### 本地 API

绑定：`http://127.0.0.1:5519`。不要把这个端口打到公网。写操作需要桌面会话 token。无 Origin 的 loopback 读取（`curl`、Photon）视为同一用户本机工具，**不是**写权限。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/healthz` | 身份、存活、桌面挑战 |
| `GET` / `PUT` | `/v1/settings` | 资料库路径与当前模型档 |
| `POST` | `/v1/indexing/jobs` | 照片文件夹索引 / 重建 |
| `POST` | `/v1/assets/import` | 发现并排队视频分析 |
| `POST` | `/v1/search/mixed` | 照片 + 带时间戳的视频片段 |
| `GET` / `PUT` | `/v1/inbox/*` | 可逆审阅元数据 |
| `GET` / `PUT` | `/v1/creator/profile*` | 版本化创作档案 |
| `POST` | `/v1/retrieval/query` | 自然语言检索 |
| `POST` | `/v1/retrieval/copy` | 有依据的标题 / 说明 |
| `POST` / `GET` | `/v1/creative/*` · `/v1/timelines/*` | 简报、修订、校验 |
| `POST` | `/v1/renders` | 绑定哈希的 720p 预览任务 |
| `GET` | `/v1/library/previews/<path>` | 浏览器可用 JPEG（HEIC 需 `pillow-heif`） |

完整路由见 `backend/src/api/routes.py`。

### Photon（Discord）

可选。走同一套 Flask 检索 API，**不是**桌面应用里的聊天。未配置 Discord 用户白名单时失败关闭；服务器消息还需要频道白名单。图片回复会 **把副本上传到 Discord**。

```bash
cd photon-bot && cp .env.example .env && npm install && npm run doctor:discord && npm run dev
```

iMessage 仅为实验路径。详见 [photon-bot/README.md](photon-bot/README.md)。

---

## 隐私

- 索引、缓存、预览和 `.env` 均已 gitignore。默认的 `./local-photo-library` 只是占位。
- **照片：** 若使用 API 视觉档，索引时会在告知后发送 **缩小后的工作副本**。要像素不出设备，请用 Ollama 或元数据回退。
- **视频：** 探测、帧、音频、转写、时间线和渲染都留在本地。照片服务商的 key **不会**授权视频出站。
- 逆地理编码（Nominatim）**默认关闭**（`ENABLE_REVERSE_GEOCODE=false`）。
- 灵感 / 文案只发送摘要和选中的事实，不发送整库，也不发送私人绝对路径。
- Inbox / Creator Memory 是版本化元数据。归档不会移动或删除文件。
- 桌面 API 为 loopback + 每次启动的 token。另存为写新文件，拒绝覆盖已有目标。

---

## 许可

Copyright © 2026 Bingjie Zhu。MemoLens 是 **源码公开（source-available）**，不是 [OSI Open Source](https://opensource.org/osd)：公开授权 **不允许** 把代码拿去做商业产品、SaaS 或收费服务。

| 用途 | 条款 |
| --- | --- |
| 个人研究、学习、爱好、教育 / 公共研究机构 | [PolyForm Noncommercial 1.0.0](LICENSE) |
| 公司产品、内部生产、SaaS、收费分发 | [需单独商业许可](COMMERCIAL-LICENSE.md) —— 联系 [Bingjie Zhu](https://github.com/bingjiezhu) |

FFmpeg 是外部运行时：[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。贡献说明：[CONTRIBUTING.md](CONTRIBUTING.md)。安全披露：[SECURITY.md](SECURITY.md)。
