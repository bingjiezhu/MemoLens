# MemoLens Photon Bot

Discord messaging bridge for MemoLens.

1. Listens for Discord messages  
2. Calls the local Flask retrieval API  
3. Replies with text and image attachments  

> iMessage helpers (`imessage.ts`, `doctor:imessage`) exist for experiments, but **Discord is the supported runtime entry** (`src/index.ts`).

## Boundary

All messaging-platform logic lives in `photon-bot/`. It does not modify `backend/`, `core/`, `indexing/`, or the React `src/` UI.

## Prerequisites

### 1. Backend online

Backend default: `http://127.0.0.1:5519`

Point the bot and backend at the same library:

```bash
IMAGE_LIBRARY_DIR=/absolute/path/to/your/photo/folder
SQLITE_DB_PATH=/absolute/path/to/your/photo/folder/photo_index.db
BACKEND_BASE_URL=http://127.0.0.1:5519
```

Smoke test:

```bash
curl -X POST http://127.0.0.1:5519/v1/retrieval/query \
  -H 'Content-Type: application/json' \
  -d '{"text":"beach sunset","top_k":3,"db_path":"/absolute/path/to/your/photo/folder/photo_index.db","image_library_dir":"/absolute/path/to/your/photo/folder"}'
```

### 2. Discord bot

- Discord application + bot token  
- **Message Content Intent** enabled  
- Bot invited to a test server  
- Optional channel allowlist: `DISCORD_ALLOWED_CHANNEL_IDS`

## Install & run

```bash
cd photon-bot
cp .env.example .env
npm install
npm run doctor:discord
npm run dev
```

Build / start:

```bash
npm run build
npm run start
```

From repo root you can also use `./start_discord.sh`.

## Environment

Required:

- `BACKEND_BASE_URL` (default `http://127.0.0.1:5519`)
- `IMAGE_LIBRARY_DIR`
- `DISCORD_BOT_TOKEN`
- `BACKEND_SEND_PATH_OVERRIDES=true` (recommended so the bot and API share the same library)

Optional: `SQLITE_DB_PATH`, `DISCORD_SEND_IMAGE_WIDTH`, `DISCORD_ALLOWED_CHANNEL_IDS`, `BACKEND_REQUEST_TIMEOUT_MS`, `DEFAULT_TOP_K`, `DEFAULT_REPLY_IMAGE_COUNT`, `SESSION_TTL_MINUTES`, `LOG_LEVEL`

See [`.env.example`](.env.example).

## Triggers & follow-ups

Responds to DMs, @mentions, and allowlisted channels.

Useful follow-ups:

- `More like this`
- `Keep only landscapes`
- `Less portraits`
- `Add night scenes`
- `Send first two originals`
