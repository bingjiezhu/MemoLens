import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  AttachmentBuilder,
  Client,
  Events,
  GatewayIntentBits,
  Partials,
  type Message,
} from "discord.js";
import sharp from "sharp";

import type { BotConfig } from "./config.js";
import { shouldHandleDiscordMessage } from "./discordAccess.js";
import { revalidateResolvedImagePath } from "./imageResolver.js";
import { requireExternalPlatformAllowed } from "./networkPolicy.js";
import { requirePhotonProductionAction } from "./productionSurfaceRegistry.js";
import type { BotReply, IncomingMessage, MessagePlatformAdapter } from "./types.js";

const MAX_ATTACHMENT_BYTES_PER_FILE = 7_500_000;
const MAX_ATTACHMENT_BYTES_PER_MESSAGE = 7_500_000;
const MAX_SOURCE_IMAGE_BYTES = 100_000_000;
const MAX_DECODED_IMAGE_PIXELS = 80_000_000;
const MAX_SEND_IMAGE_WIDTH = 4096;

export type PreparedAttachment = {
  uploadBytes: Buffer;
  fileSize: number;
  uploadName: string;
};

export type DiscordImageEncoder = (
  sourceBytes: Buffer,
  outputPath: string,
  targetWidth: number,
) => Promise<void>;

export type AttachmentPreparationResult = {
  attachments: PreparedAttachment[];
  rejectedCount: number;
};

type AttachmentPreparationOptions = {
  encoder?: DiscordImageEncoder;
  maxAttachmentBytes?: number;
  tempRoot?: string;
};

type DiscordAdmissionRecord = {
  chatId: string;
  replyConsumed: boolean;
  userId: string;
  workerConsumed: boolean;
};

export type DiscordAdapterOptions = {
  /** Test/runtime seam; production always uses the default discord.js client. */
  client?: Client;
};

export class DiscordAdapter implements MessagePlatformAdapter {
  private readonly client: Client;
  private readonly admissions = new WeakMap<object, DiscordAdmissionRecord>();
  private started = false;

  constructor(
    private readonly config: Pick<
      BotConfig,
      | "discordBotToken"
      | "discordExpectedBotUserId"
      | "discordAllowedUserIds"
      | "discordAllowedChannelIds"
      | "discordSendImageWidth"
      | "imageLibraryDir"
      | "logLevel"
    >,
    options: DiscordAdapterOptions = {},
  ) {
    this.client =
      options.client ??
      new Client({
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
    this.requireConfiguredInboundAuthority();
    this.requireProviderCredential();
    requirePhotonProductionAction("photon_bot.discord.connect");
    requireExternalPlatformAllowed("discord");

    await this.client.login(this.config.discordBotToken);
    if (this.client.user?.id !== this.config.discordExpectedBotUserId) {
      this.client.destroy();
      throw new Error(
        "Discord account scope denied: authenticated bot user does not match DISCORD_EXPECTED_BOT_USER_ID.",
      );
    }

    this.client.on(Events.Error, (error) => {
      handlers.onError(toError(error));
    });

    this.client.on(Events.MessageCreate, async (message) => {
      const botUserId = this.client.user?.id;
      if (
        !shouldHandleDiscordMessage(
          {
            authorId: message.author.id,
            channelId: message.channelId,
            isAuthorBot: message.author.bot,
            isSystemMessage: message.system,
            isDirectMessage: message.channel.isDMBased(),
            mentionsBot: botUserId ? message.mentions.users.has(botUserId) : false,
          },
          {
            allowedUserIds: this.config.discordAllowedUserIds,
            allowedChannelIds: this.config.discordAllowedChannelIds,
          },
        )
      ) {
        return;
      }

      const incoming = toIncomingMessage(message, botUserId);
      if (!incoming) {
        return;
      }

      try {
        requireExternalPlatformAllowed("discord");
        requirePhotonProductionAction("photon_bot.discord.receive_message");
        const admitted = this.admitIncomingMessage(incoming);
        await handlers.onMessage(admitted);
      } catch (error) {
        handlers.onError(toError(error));
      }
    });

    this.started = true;
  }

  requireAdmittedMessage(message: IncomingMessage): void {
    const record = this.admissionRecord(message.admission);
    if (
      !this.started ||
      record.replyConsumed ||
      record.workerConsumed ||
      record.chatId !== message.chatId ||
      record.userId !== message.userId
    ) {
      throw new Error(
        "Discord worker admission denied: the message was not admitted by this adapter.",
      );
    }
    record.workerConsumed = true;
  }

  async sendReply(chatId: string, reply: BotReply, admission?: object): Promise<void> {
    if (reply.imagePaths.length > 0) {
      requirePhotonProductionAction("photon_bot.discord.send_attachments");
    } else {
      requirePhotonProductionAction("photon_bot.discord.send_reply");
    }
    this.consumeReplyAdmission(chatId, admission);
    this.requireProviderCredential();
    if (!this.started) {
      throw new Error("Discord reply denied: the authenticated adapter is not running.");
    }
    requireExternalPlatformAllowed("discord");

    const preparation =
      reply.imagePaths.length > 0
        ? await prepareAttachmentsForDiscord(
            this.config.imageLibraryDir,
            reply.imagePaths,
            this.config.discordSendImageWidth,
          )
        : { attachments: [], rejectedCount: 0 };
    const attachmentPlan = planAttachmentBatches(preparation.attachments);
    const rejectedCount = preparation.rejectedCount + attachmentPlan.skippedCount;
    if (rejectedCount > 0 || preparation.attachments.length !== reply.imagePaths.length) {
      throw new Error(
        "Discord attachment egress denied: every source must pass identity and derived-artifact validation.",
      );
    }

    const channel = await this.client.channels.fetch(chatId);
    if (!channel || !channel.isTextBased() || !("send" in channel)) {
      throw new Error(`Channel is not sendable: ${chatId}`);
    }

    const primaryContent = reply.text.trim();

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
  }

  async close(): Promise<void> {
    if (!this.started) {
      return;
    }

    this.client.destroy();
    this.started = false;
  }

  private admitIncomingMessage(message: IncomingMessage): IncomingMessage {
    const admission = Object.freeze(Object.create(null)) as object;
    this.admissions.set(admission, {
      chatId: message.chatId,
      replyConsumed: false,
      userId: message.userId,
      workerConsumed: false,
    });
    return { ...message, admission };
  }

  private admissionRecord(admission: object | undefined): DiscordAdmissionRecord {
    if (!admission) {
      throw new Error(
        "Discord worker admission denied: an adapter-minted capability is required.",
      );
    }
    const record = this.admissions.get(admission);
    if (!record) {
      throw new Error(
        "Discord worker admission denied: the capability is unknown to this adapter.",
      );
    }
    return record;
  }

  private consumeReplyAdmission(chatId: string, admission: object | undefined): void {
    const record = this.admissionRecord(admission);
    if (record.replyConsumed || record.chatId !== chatId) {
      throw new Error(
        "Discord worker admission denied: the reply capability is stale or out of scope.",
      );
    }
    record.replyConsumed = true;
  }

  private requireConfiguredInboundAuthority(): void {
    if (this.config.discordAllowedUserIds.length === 0) {
      throw new Error(
        "Discord receive authority denied: at least one allowed user is required.",
      );
    }
  }

  private requireProviderCredential(): void {
    if (!this.config.discordBotToken.trim()) {
      throw new Error(
        "Discord provider credential denied: DISCORD_BOT_TOKEN must be set.",
      );
    }
  }
}

export async function prepareAttachmentsForDiscord(
  imageLibraryDir: string,
  imagePaths: readonly string[],
  targetWidth: number,
  options: AttachmentPreparationOptions = {},
): Promise<AttachmentPreparationResult> {
  const attachments: PreparedAttachment[] = [];
  let rejectedCount = 0;

  if (
    !Number.isSafeInteger(targetWidth) ||
    targetWidth < 1 ||
    targetWidth > MAX_SEND_IMAGE_WIDTH
  ) {
    return { attachments, rejectedCount: imagePaths.length };
  }

  const maxAttachmentBytes = options.maxAttachmentBytes ?? MAX_ATTACHMENT_BYTES_PER_FILE;
  if (!Number.isSafeInteger(maxAttachmentBytes) || maxAttachmentBytes < 1) {
    return { attachments, rejectedCount: imagePaths.length };
  }

  for (const imagePath of imagePaths) {
    try {
      const prepared = await prepareAttachment(
        imageLibraryDir,
        imagePath,
        targetWidth,
        maxAttachmentBytes,
        options,
      );
      attachments.push({
        ...prepared,
        uploadName: `memolens-image-${attachments.length + 1}.jpg`,
      });
    } catch {
      rejectedCount += 1;
    }
  }

  return { attachments, rejectedCount };
}

async function prepareAttachment(
  imageLibraryDir: string,
  imagePath: string,
  targetWidth: number,
  maxAttachmentBytes: number,
  options: AttachmentPreparationOptions,
): Promise<Omit<PreparedAttachment, "uploadName">> {
  const sourceBytes = await readTrustedSourceImage(imageLibraryDir, imagePath);
  const configuredTempRoot = path.resolve(options.tempRoot ?? os.tmpdir());
  const tempRootStat = await fs.promises.lstat(configuredTempRoot);
  if (tempRootStat.isSymbolicLink() || !tempRootStat.isDirectory()) {
    throw new Error("Attachment temp root must be a real directory.");
  }
  const realTempRoot = await fs.promises.realpath(configuredTempRoot);
  const tempDir = await fs.promises.mkdtemp(path.join(realTempRoot, "memolens-discord-"));
  const outputPath = path.join(tempDir, "attachment.jpg");

  try {
    await fs.promises.chmod(tempDir, 0o700);
    const encoder = options.encoder ?? encodeRestrictedJpeg;
    await encoder(sourceBytes, outputPath, targetWidth);

    const outputStat = await fs.promises.lstat(outputPath);
    if (outputStat.isSymbolicLink() || !outputStat.isFile()) {
      throw new Error("JPEG encoder did not create a regular file.");
    }
    if (outputStat.size < 1 || outputStat.size > maxAttachmentBytes) {
      throw new Error("Encoded JPEG is outside the upload size boundary.");
    }

    await fs.promises.chmod(outputPath, 0o600);
    const uploadBytes = await fs.promises.readFile(outputPath);
    if (uploadBytes.length !== outputStat.size || !hasJpegEnvelope(uploadBytes)) {
      throw new Error("Encoded attachment is not a complete JPEG.");
    }

    const metadata = await sharp(uploadBytes, {
      failOn: "error",
      limitInputPixels: MAX_DECODED_IMAGE_PIXELS,
    }).metadata();
    if (
      metadata.format !== "jpeg" ||
      metadata.width === undefined ||
      metadata.width < 1 ||
      metadata.width > targetWidth ||
      metadata.height === undefined ||
      metadata.height < 1
    ) {
      throw new Error("Encoded attachment failed JPEG verification.");
    }

    return { uploadBytes, fileSize: uploadBytes.length };
  } finally {
    await fs.promises.rm(tempDir, {
      force: true,
      recursive: true,
      maxRetries: 3,
      retryDelay: 25,
    });
  }
}

async function readTrustedSourceImage(
  imageLibraryDir: string,
  imagePath: string,
): Promise<Buffer> {
  const trustedPath = revalidateResolvedImagePath(imageLibraryDir, imagePath);
  const noFollowFlag = fs.constants.O_NOFOLLOW ?? 0;
  const handle = await fs.promises.open(trustedPath, fs.constants.O_RDONLY | noFollowFlag);

  try {
    const before = await handle.stat();
    if (!before.isFile() || before.size < 1 || before.size > MAX_SOURCE_IMAGE_BYTES) {
      throw new Error("Source image is not a bounded regular file.");
    }

    const revalidatedPath = revalidateResolvedImagePath(imageLibraryDir, trustedPath);
    const pathStat = await fs.promises.lstat(revalidatedPath);
    if (
      pathStat.isSymbolicLink() ||
      !pathStat.isFile() ||
      pathStat.dev !== before.dev ||
      pathStat.ino !== before.ino ||
      pathStat.size !== before.size
    ) {
      throw new Error("Source image changed during boundary validation.");
    }

    const sourceBytes = await handle.readFile();
    const after = await handle.stat();
    if (
      after.dev !== before.dev ||
      after.ino !== before.ino ||
      after.size !== before.size ||
      after.mtimeMs !== before.mtimeMs ||
      after.ctimeMs !== before.ctimeMs ||
      sourceBytes.length !== before.size
    ) {
      throw new Error("Source image changed while it was being read.");
    }
    return sourceBytes;
  } finally {
    await handle.close();
  }
}

async function encodeRestrictedJpeg(
  sourceBytes: Buffer,
  outputPath: string,
  targetWidth: number,
): Promise<void> {
  await sharp(sourceBytes, {
    failOn: "error",
    limitInputPixels: MAX_DECODED_IMAGE_PIXELS,
    sequentialRead: true,
  })
    .rotate()
    .resize({
      width: targetWidth,
      fit: "inside",
      withoutEnlargement: true,
    })
    .flatten({ background: "#ffffff" })
    .jpeg({
      chromaSubsampling: "4:2:0",
      force: true,
      progressive: true,
      quality: 85,
    })
    .toFile(outputPath);
}

function hasJpegEnvelope(bytes: Buffer): boolean {
  return (
    bytes.length >= 4 &&
    bytes[0] === 0xff &&
    bytes[1] === 0xd8 &&
    bytes[bytes.length - 2] === 0xff &&
    bytes[bytes.length - 1] === 0xd9
  );
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

export function planAttachmentBatches(attachments: readonly PreparedAttachment[]): {
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

export function buildMessagePayload(input: {
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
        new AttachmentBuilder(attachment.uploadBytes, {
          name: attachment.uploadName,
        }),
    ),
  };

  if (input.content.trim()) {
    payload.content = input.content.trim();
  }

  return payload;
}
