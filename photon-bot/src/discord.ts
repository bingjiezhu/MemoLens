import { execFile } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

import {
  AttachmentBuilder,
  Client,
  Events,
  GatewayIntentBits,
  Partials,
  type Message,
} from "discord.js";

import type { BotConfig } from "./config.js";
import type { BotReply, IncomingMessage, MessagePlatformAdapter } from "./types.js";

const MAX_ATTACHMENT_BYTES_PER_FILE = 7_500_000;
const MAX_ATTACHMENT_BYTES_PER_MESSAGE = 7_500_000;
const execFileAsync = promisify(execFile);

type PreparedAttachment = {
  uploadPath: string;
  fileSize: number;
  cleanupDir: string | null;
};

export class DiscordAdapter implements MessagePlatformAdapter {
  private readonly client: Client;
  private started = false;

  constructor(
    private readonly config: Pick<
      BotConfig,
      "discordBotToken" | "discordAllowedChannelIds" | "discordSendImageWidth" | "logLevel"
    >,
  ) {
    this.client = new Client({
      intents: [
        GatewayIntentBits.Guilds,
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.DirectMessages,
        GatewayIntentBits.MessageContent,
      ],
      partials: [Partials.Channel],
    });
  }

  async startWatching(handlers: {
    onMessage: (message: IncomingMessage) => Promise<void>;
    onError: (error: Error) => void;
  }): Promise<void> {
    if (this.started) {
      return;
    }

    this.client.on(Events.Error, (error) => {
      handlers.onError(toError(error));
    });

    this.client.on(Events.MessageCreate, async (message) => {
      if (!shouldHandleMessage(message, this.client.user?.id, this.config.discordAllowedChannelIds)) {
        return;
      }

      const incoming = toIncomingMessage(message, this.client.user?.id);
      if (!incoming) {
        return;
      }

      try {
        await handlers.onMessage(incoming);
      } catch (error) {
        handlers.onError(toError(error));
      }
    });

    await this.client.login(this.config.discordBotToken);
    this.started = true;
  }

  async sendReply(chatId: string, reply: BotReply): Promise<void> {
    const channel = await this.client.channels.fetch(chatId);
    if (!channel || !channel.isTextBased() || !("send" in channel)) {
      throw new Error(`Channel is not sendable: ${chatId}`);
    }

    const preparedAttachments = await prepareAttachmentsForDiscord(
      reply.imagePaths,
      this.config.discordSendImageWidth,
    );

    try {
      const attachmentPlan = planAttachmentBatches(preparedAttachments);
      const skippedNotice =
        attachmentPlan.skippedCount > 0
          ? `\n\nSkipped ${attachmentPlan.skippedCount} oversized image${attachmentPlan.skippedCount === 1 ? "" : "s"} after resize because Discord still rejected the file size.`
          : "";
      const primaryContent = `${reply.text.trim()}${skippedNotice}`.trim();

      if (attachmentPlan.batches.length === 0) {
        await channel.send(primaryContent || "No uploadable images were available for this result.");
        return;
      }

      await channel.send(
        buildMessagePayload({
          content: primaryContent,
          attachments: attachmentPlan.batches[0]!,
        }),
      );

      for (let index = 1; index < attachmentPlan.batches.length; index += 1) {
        await channel.send(
          buildMessagePayload({
            content: `More images (${index + 1}/${attachmentPlan.batches.length})`,
            attachments: attachmentPlan.batches[index]!,
          }),
        );
      }
    } finally {
      await cleanupPreparedAttachments(preparedAttachments);
    }
  }

  async close(): Promise<void> {
    if (!this.started) {
      return;
    }

    this.client.destroy();
    this.started = false;
  }
}

function shouldHandleMessage(
  message: Message,
  botUserId: string | undefined,
  allowedChannelIds: readonly string[],
): boolean {
  if (message.author.bot || message.system) {
    return false;
  }

  if (message.channel.isDMBased()) {
    return true;
  }

  if (allowedChannelIds.includes(message.channelId)) {
    return true;
  }

  return botUserId ? message.mentions.users.has(botUserId) : false;
}

function toIncomingMessage(
  message: Message,
  botUserId: string | undefined,
): IncomingMessage | null {
  const text = sanitizeMessageText(message.content, botUserId);
  if (!text) {
    return null;
  }

  return {
    chatId: message.channelId,
    userId: message.author.id,
    senderName:
      message.member?.displayName ?? message.author.globalName ?? message.author.username,
    text,
    receivedAt: message.createdAt.toISOString(),
  };
}

function sanitizeMessageText(content: string, botUserId: string | undefined): string {
  const trimmed = content.trim();
  if (!trimmed) {
    return "";
  }

  if (!botUserId) {
    return trimmed;
  }

  return trimmed.replace(new RegExp(`<@!?${botUserId}>`, "g"), "").trim();
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

async function prepareAttachmentsForDiscord(
  imagePaths: readonly string[],
  targetWidth: number,
): Promise<PreparedAttachment[]> {
  const attachments: PreparedAttachment[] = [];

  for (const imagePath of imagePaths) {
    attachments.push(await prepareAttachment(imagePath, targetWidth));
  }

  return attachments;
}

async function prepareAttachment(
  imagePath: string,
  targetWidth: number,
): Promise<PreparedAttachment> {
  const tempDir = await fs.promises.mkdtemp(
    path.join(os.tmpdir(), "memolens-discord-"),
  );
  const extension = path.extname(imagePath) || ".jpg";
  const baseName = path.basename(imagePath, extension);
  const uploadPath = path.join(tempDir, `${baseName}-w${targetWidth}${extension}`);

  try {
    await execFileAsync("sips", [
      "--resampleWidth",
      String(targetWidth),
      imagePath,
      "--out",
      uploadPath,
    ]);
  } catch {
    await fs.promises.rm(tempDir, { force: true, recursive: true });
    const originalStat = await fs.promises.stat(imagePath);
    return {
      uploadPath: imagePath,
      fileSize: originalStat.size,
      cleanupDir: null,
    };
  }

  const stat = await fs.promises.stat(uploadPath);
  return {
    uploadPath,
    fileSize: stat.size,
    cleanupDir: tempDir,
  };
}

async function cleanupPreparedAttachments(
  attachments: readonly PreparedAttachment[],
): Promise<void> {
  const cleanupDirs = [
    ...new Set(
      attachments
        .map((item) => item.cleanupDir)
        .filter((cleanupDir): cleanupDir is string => cleanupDir !== null),
    ),
  ];
  await Promise.all(
    cleanupDirs.map((cleanupDir) =>
      fs.promises.rm(cleanupDir, { force: true, recursive: true }),
    ),
  );
}

function planAttachmentBatches(attachments: readonly PreparedAttachment[]): {
  batches: PreparedAttachment[][];
  skippedCount: number;
} {
  const batches: PreparedAttachment[][] = [];
  let skippedCount = 0;
  let currentBatch: PreparedAttachment[] = [];
  let currentBatchBytes = 0;

  for (const attachment of attachments) {
    if (attachment.fileSize > MAX_ATTACHMENT_BYTES_PER_FILE) {
      skippedCount += 1;
      continue;
    }

    if (
      currentBatch.length > 0 &&
      currentBatchBytes + attachment.fileSize > MAX_ATTACHMENT_BYTES_PER_MESSAGE
    ) {
      batches.push(currentBatch);
      currentBatch = [];
      currentBatchBytes = 0;
    }

    currentBatch.push(attachment);
    currentBatchBytes += attachment.fileSize;
  }

  if (currentBatch.length > 0) {
    batches.push(currentBatch);
  }

  return {
    batches,
    skippedCount,
  };
}

function buildMessagePayload(input: {
  content: string;
  attachments: readonly PreparedAttachment[];
}): {
  content?: string;
  files: AttachmentBuilder[];
} {
  const payload: {
    content?: string;
    files: AttachmentBuilder[];
  } = {
    files: input.attachments.map(
      (attachment) =>
        new AttachmentBuilder(attachment.uploadPath, {
          name: path.basename(attachment.uploadPath),
        }),
    ),
  };

  if (input.content.trim()) {
    payload.content = input.content.trim();
  }

  return payload;
}
