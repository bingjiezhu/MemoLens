<p align="right"><a href="README.md">English</a> · <strong>简体中文</strong></p>

# MemoLens

**创作者的私人素材家 —— 本地记住，等你下次要发内容时再用。**

把照片和视频放在你本来就在用的文件夹。在 **Codex 或 DeepSeek Harness** 里讨论项目，再打开共享的浏览器剪辑器精修已保存的时间线。本地 companion 负责目录确认、后台扫描、配对和导出，不再建立另一套 AI 聊天。原件不动，保存产生新版本。

**开发预览版：** 已实现本地扫描、有素材依据的初剪、可撤回编辑和 **1080p 静音硬切作品包**。声音混音、字幕、转场以及一句话到完整成片的流程尚未完成。先看[使用与恢复指南](docs/user-guide.md)，再看[本次验证与剩余边界](docs/releases/2026-10-02-readiness.md)。

**许可。** 源码公开（source-available）双许可：[非商业 PolyForm Noncommercial 1.0.0](LICENSE) · [商业使用需单独授权](COMMERCIAL-LICENSE.md)。

<p align="center">
  <a href="https://github.com/bingjiezhu/MemoLens/releases/download/promo/memolens-promo.mp4">
    <img src="docs/assets/memolens-promo-poster.jpg" alt="观看 50 秒 MemoLens 流程：本地记住、Inbox 过片、找到瞬间、做出初剪" width="100%" />
  </a>
</p>

<p align="center"><sub>历史 0.5 桌面版演示，不代表当前插件流程。<a href="https://github.com/bingjiezhu/MemoLens/releases/download/promo/memolens-promo.mp4">播放 MP4</a> · <a href="docs/assets/memolens-promo.mp4">下载</a></sub></p>

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

Agent、浏览器剪辑器和原生 companion 共用一个项目。上方截图保留 0.5 桌面版的视觉参考。

| 房间 | 做什么 |
| --- | --- |
| **Library** | 原生目录确认；插件引导建立可恢复的照片/视频扫描。Inbox 审阅和经确认的 Creator Memory 保持本地、可撤回。 |
| **Memories** | 从同一份 SQLite 索引里重访主题、关键词星系、重复组和收藏篮。 |
| **Create** | 按准确项目 ID 打开已有项目，检查 Blueprint、生成 Coverage 和 Timeline，再申请原生导出。旧简报/720p 预览流程单独保留。 |
| **浏览器剪辑器** | 选择、定位、裁剪、重排、替换、分割、移除、保存/放弃、历史查看/恢复；写入需要该项目的独立配对授权。 |
| **Home** | Inbox 与 Creator Memory 的摘要，让下一步动作一眼能看清。 |

另外还包括：

- 自然语言检索（含排除词）、质量感知排序、近重复抑制
- Codex 与 DeepSeek 共用已保存的时间线；独立 Unsaved Draft Lab 仅作未保存实验，不会写入正式项目
- 正式导出包含视频、文稿、manifest 和准确使用清单；不覆盖原素材或已有作品包
- 可选视觉 / 查询模型档（MiniMax、Vertex/Gemini、OpenAI 兼容、DashScope、Ollama）。没有 API key 也能跑：元数据与 semantic-hash 回退仍然可用
- 可选 [Photon](photon-bot/README.md) Discord 桥，走同一套本地 API（**不是**应用内聊天）

**当前边界：** 元数据/semantic-hash 回退不是视觉理解。浏览器源预览强制静音，正式 1080p 导出也是静音硬切。旧 720p 预览是独立兼容路径。完整声音、字幕、转场、可迁移编辑项目包和真实模型跨宿主验收仍未完成。逆地理编码默认关闭。

---

## 快速开始

**环境：** 原生 companion 使用 macOS · Node.js 22.12+ · 带有通过安全检查的 SQLite 的 Python 3.10+ · FFmpeg/ffprobe 6+。执行下列命令前，先安装 [Homebrew](https://brew.sh) 和 Node.js。macOS 已验证使用 Homebrew Python 3.14，但仍会检查它实际链接的 SQLite。Setup 只创建项目虚拟环境并安装依赖，不负责安装 Python 或 Node.js。独立插件支持 Python 3.10+；DeepSeek Harness 还需要 Node.js 22.19+ 或 24+。

```bash
git clone https://github.com/bingjiezhu/MemoLens.git
cd MemoLens
cp .env.example .env          # 可选：填服务商 key 或改用 Ollama
brew install python@3.14 ffmpeg
MEMOLENS_PYTHON="$(brew --prefix python@3.14)/bin/python3.14" npm run setup:mac
./Launch\ MemoLens.command
```

完成 setup 后也可以 `npm run electron`。

### 安装 Agent 插件

在仓库目录执行，需已安装 Codex CLI：

```bash
source .venv/bin/activate     # 从此终端启动 CLI 宿主，使用 Python 3.10+
python3 --version
codex plugin marketplace add "$(pwd)"
codex plugin add memolens@memolens-local
```

两个适配器都从宿主进程的 PATH 启动 `python3`；`MEMOLENS_PYTHON` 只选择 Core 的安装环境。使用 Codex Desktop 时，其插件进程也必须能找到 Python 3.10+；在另一个终端激活虚拟环境，不会改变已经运行的桌面应用。

新建 Codex 任务，请 MemoLens 设置 Library。收到提示后打开原生 companion，在系统对话框里选择文件夹；扫描、配对和编辑期间保持 companion 运行。DeepSeek Harness Web 安装方式：

```bash
dsh plugin --profile web add "$(pwd)/.agents/plugins/plugins/memolens"
dsh --profile web --dump-config
dsh --profile web
```

DeepSeek 适配目前是开发预览，不能保证所有 Harness 版本兼容。见[准确版本和宿主说明](.agents/plugins/plugins/memolens/deepseek-harness/README.md)。

### 第一个项目

1. 请 Agent 查询 Library 扫描状态和准确项目 ID。扫描完成不等于已有创作方案或可编辑时间线。
2. 与 Agent 形成引用具体素材证据的 Blueprint，在原生配对框中只批准所需项目操作。
3. 在 **Create → Video first cut → Open existing project** 输入该 ID。检查方案、生成 Coverage 和初剪；缺证据的地方保留缺口，不用无关素材补齐。
4. 请 Agent 在 **MemoLens Canonical Editor** 中打开同一项目。Save 写新版本，Discard 不写入。导出在原生项目工作区执行，目前为静音作品包。

[使用指南](docs/user-guide.md)说明配对过期、缺素材和恢复方法。插件不需要另一份 MemoLens 模型 key；自动视觉分析是独立可选的服务商能力。

**可选的原桌面流程**

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

**浏览器开发模式**（不能替代原生目录或导出权限）：

```bash
npm run setup:mac
bash scripts/run_python.sh backend/app.py  # http://127.0.0.1:5519
npm run dev                   # http://127.0.0.1:5173
```

诊断时使用独立临时 app-state 和预先配置的测试库。正常目录权限来自原生选择器，不来自任意浏览器路径。开发栈：`npm run dev:local`。

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

仅用于明确隔离的无界面开发：

```bash
export IMAGE_LIBRARY_DIR="/absolute/path/to/your/photos"
export SQLITE_DB_PATH="/absolute/path/to/disposable-state/photo_index.db"
```

当前资料库使用应用内受管索引/重建流程。旧的直接 backfill 脚本拒绝受管数据库，不能用来修改正式分析结果。

---

## 架构

<p align="center">
  <img src="docs/assets/memolens-workspaces.png" alt="MemoLens 0.5 四个房间：Home、Library、Memories、Create" width="100%" />
</p>

<p align="center">
  <img src="docs/assets/memolens-architecture.png" alt="MemoLens 架构：从界面到 SQLite 的本地分层" width="100%" />
</p>

<p align="center"><sub>历史桌面架构画板；当前职责与项目链见下文。</sub></p>

```text
Library → 素材证据 → Blueprint → Coverage → Timeline → 静音导出 → Usage
                              ↑                   ↑
                    Codex / DeepSeek        共享浏览器剪辑器
                              └──── 同一个本地 Core ────┘
                           原生 companion：权限与后台运行
```

| 层 | 位置 | 职责 |
| --- | --- | --- |
| 界面 | `src/` | Home、Library、Memories、Create |
| 桌面 | `electron/` | 文件夹 / 另存为选择器、Application Support 里的 SQLite、Flask 监管、IPC |
| API | `backend/` | 本机 HTTP；照片索引与视频导入是不同路由 |
| 智能 | `indexing/`、`backend/src/retrieval/`、`backend/src/media/`、`core/` | 照片视觉、混合检索、Inbox、导演、时间线、720p 渲染 |
| 数据 | `core/db.py`、`core/media_db.py` | 受管图片/媒体 schema v20、不可变版本与导出使用事实；原片不覆盖 |
| Agent 适配 | `.agents/plugins/plugins/memolens/` | 共享只读工具、独立配对写入和项目绑定的浏览器剪辑器 |

React companion 界面在仓库根目录 `src/`；`frontend/` 是遗留 Python 兼容层，不是 UI。Agent 和浏览器共用 Core 合同；只读访问不授予原生目录、导出或发布权限。实现与剩余工作见[规范索引](docs/specs/README.md)。

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
| `GET` / `PUT` | `/v1/settings` | 读取设置 / 有边界的模型档更新，不授予任意目录权限 |
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
