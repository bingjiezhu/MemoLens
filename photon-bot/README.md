# MemoLens Photon Bot

这个目录现在是 MemoLens 的 Discord 消息接入层。

它做三件事：

1. 监听 Discord 消息
2. 调现有 Flask API 做检索
3. 把结果作为聊天文本和图片附件回到 Discord

## 目录边界

所有消息平台相关逻辑都在 `photon-bot/`。

它不会改动原有的：

- `backend/`
- `frontend/`
- `core/`
- `indexing/`

## 运行前提

### 1. 先把后端跑起来

后端需要能访问你本地的图片目录和 `photo_index.db`。

如果你想让后端和 bot 都指向同一套本地图库，可以让它们共享同一个图片目录和 SQLite 文件，例如：

- `IMAGE_LIBRARY_DIR=/absolute/path/to/your/photo/folder`
- `SQLITE_DB_PATH=/absolute/path/to/your/photo/folder/photo_index.db`

接口联调示例：

```bash
curl -X POST http://127.0.0.1:5519/v1/retrieval/query \
  -H 'Content-Type: application/json' \
  -d '{"text":"beach sunset","top_k":3,"db_path":"/absolute/path/to/your/photo/folder/photo_index.db","image_library_dir":"/absolute/path/to/your/photo/folder"}'
```

### 2. 准备 Discord Bot

你需要：

- 一个 Discord application
- 一个 bot token
- 打开 `Message Content Intent`
- 把 bot 邀请进测试 server

如果你只想让它在某个频道工作，可以把频道 id 写进 `DISCORD_ALLOWED_CHANNEL_IDS`。

## 安装

在 `photon-bot/` 目录执行：

```bash
npm install
```

## 环境变量

在 `photon-bot/.env` 里至少填写：

- `BACKEND_BASE_URL`
- `IMAGE_LIBRARY_DIR`
- `BACKEND_SEND_PATH_OVERRIDES=true`
- `DISCORD_BOT_TOKEN`

可选：

- `SQLITE_DB_PATH`
- `BACKEND_SEND_PATH_OVERRIDES`
- `DISCORD_SEND_IMAGE_WIDTH`
- `DISCORD_ALLOWED_CHANNEL_IDS`
- `BACKEND_REQUEST_TIMEOUT_MS`
- `DEFAULT_TOP_K`
- `DEFAULT_REPLY_IMAGE_COUNT`
- `SESSION_TTL_MINUTES`
- `LOG_LEVEL`

示例见 [`.env.example`](.env.example)。

## 启动

先检查 Discord token 是否能登录：

```bash
npm run doctor:discord
```

开发模式：

```bash
npm run dev
```

编译：

```bash
npm run build
```

编译后启动：

```bash
npm run start
```

## 消息触发规则

bot 会响应这些来源：

- 私信 bot
- 在服务器里 `@mention` bot
- 或者你把频道 id 加进 `DISCORD_ALLOWED_CHANNEL_IDS`

## 当前支持的 follow-up

- `再来一组`
- `只保留风景`
- `少一点人像`
- `要夜景`
- `发前两张原图`
