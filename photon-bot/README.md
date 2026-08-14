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
# Copy the exact SQLite path shown in MemoLens Setup; desktop-managed names are hashed.
SQLITE_DB_PATH=/absolute/path/to/your/Application-Support/storage/photo-index-abc123.db
BACKEND_BASE_URL=http://127.0.0.1:5519
```

Smoke test:

```bash
curl -X POST http://127.0.0.1:5519/v1/retrieval/query \
  -H 'Content-Type: application/json' \
  -d '{"text":"beach sunset","top_k":3,"db_path":"/absolute/path/to/your/Application-Support/storage/photo-index-abc123.db","image_library_dir":"/absolute/path/to/your/photo/folder"}'
```

### 2. Discord bot

- Discord application + bot token  
- **Message Content Intent** enabled  
- Bot invited to a test server  
- Required user allowlist: `DISCORD_ALLOWED_USER_IDS`
- Optional guild-channel allowlist: `DISCORD_ALLOWED_CHANNEL_IDS`

Turn on Discord Developer Mode, then copy the IDs of every trusted user and
guild channel you intend to allow. Usernames and display names are not accepted.

## Security and privacy boundary

MemoLens fails closed. `DISCORD_ALLOWED_USER_IDS` must contain at least one ID or
the bot and `doctor:discord` exit with an error. Every inbound message must come
from an allowlisted user:

- DMs need an allowlisted user; no channel entry is needed.
- Guild messages need both an allowlisted user and an allowlisted channel.
- An empty `DISCORD_ALLOWED_CHANNEL_IDS` disables every guild message.
- Mentioning the bot does not bypass either allowlist.
- Bot-authored and Discord system messages are ignored.

**Image replies upload copies of matching local photos to Discord.** Those image
files leave the local machine and are then subject to Discord's storage, access,
and retention behavior. Only add Discord users and channels where everyone who
can view the reply is trusted to see the selected photos. If local resizing is
unavailable, the bot may upload the original file when it is within the upload
size limit.

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
- `DISCORD_ALLOWED_USER_IDS` (comma-separated trusted Discord user IDs; must not be empty)
- `BACKEND_SEND_PATH_OVERRIDES=true` (recommended so the bot and API share the same library)

Optional: `SQLITE_DB_PATH`, `DISCORD_SEND_IMAGE_WIDTH`, `DISCORD_ALLOWED_CHANNEL_IDS` (comma-separated guild channel IDs; empty means DM-only), `BACKEND_REQUEST_TIMEOUT_MS`, `DEFAULT_TOP_K`, `DEFAULT_REPLY_IMAGE_COUNT`, `SESSION_TTL_MINUTES`, `LOG_LEVEL`

See [`.env.example`](.env.example).

## Triggers & follow-ups

Responds to allowlisted users in DMs, plus allowlisted users in allowlisted
guild channels. An `@mention` alone never triggers the bot.

Useful follow-ups:

- `More like this`
- `Keep only landscapes`
- `Less portraits`
- `Add night scenes`
- `Send first two originals`
