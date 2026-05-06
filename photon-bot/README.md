# MemoLens Photon Bot

This directory contains the Discord messaging bridge for MemoLens.

It does three things:

1. Listens for Discord messages.
2. Calls the existing Flask API for photo retrieval.
3. Sends chat text and image attachments back to Discord.

## Directory Boundary

All messaging-platform logic lives in `photon-bot/`.

It does not modify the existing:

- `backend/`
- `frontend/`
- `core/`
- `indexing/`

## Prerequisites

### 1. Start the Backend First

The backend must be able to access your local photo folder and `photo_index.db`.

To make the backend and bot use the same local library, point both of them to the same image folder and SQLite file, for example:

- `IMAGE_LIBRARY_DIR=/absolute/path/to/your/photo/folder`
- `SQLITE_DB_PATH=/absolute/path/to/your/photo/folder/photo_index.db`

API smoke-test example:

```bash
curl -X POST http://127.0.0.1:5519/v1/retrieval/query \
  -H 'Content-Type: application/json' \
  -d '{"text":"beach sunset","top_k":3,"db_path":"/absolute/path/to/your/photo/folder/photo_index.db","image_library_dir":"/absolute/path/to/your/photo/folder"}'
```

### 2. Prepare a Discord Bot

You need:

- A Discord application.
- A bot token.
- `Message Content Intent` enabled.
- The bot invited into a test server.

To limit the bot to specific channels, set `DISCORD_ALLOWED_CHANNEL_IDS`.

## Install

Run this from `photon-bot/`:

```bash
npm install
```

## Environment Variables

Add at least these values to `photon-bot/.env`:

- `BACKEND_BASE_URL`
- `IMAGE_LIBRARY_DIR`
- `BACKEND_SEND_PATH_OVERRIDES=true`
- `DISCORD_BOT_TOKEN`

Optional values:

- `SQLITE_DB_PATH`
- `BACKEND_SEND_PATH_OVERRIDES`
- `DISCORD_SEND_IMAGE_WIDTH`
- `DISCORD_ALLOWED_CHANNEL_IDS`
- `BACKEND_REQUEST_TIMEOUT_MS`
- `DEFAULT_TOP_K`
- `DEFAULT_REPLY_IMAGE_COUNT`
- `SESSION_TTL_MINUTES`
- `LOG_LEVEL`

See [`.env.example`](.env.example) for an example.

## Run

First verify that the Discord token can log in:

```bash
npm run doctor:discord
```

Development mode:

```bash
npm run dev
```

Build:

```bash
npm run build
```

Start after building:

```bash
npm run start
```

## Message Triggers

The bot responds to:

- Direct messages.
- Server messages that mention the bot.
- Messages in channels listed in `DISCORD_ALLOWED_CHANNEL_IDS`.

## Supported Follow-Ups

- `More like this`
- `Keep only landscapes`
- `Less portraits`
- `Add night scenes`
- `Send first two originals`
