import { IMessageSDK } from "@photon-ai/imessage-kit";

import type { LogLevel } from "./config.js";
import type { BotReply, IncomingMessage, MessagePlatformAdapter } from "./types.js";

type PhotonMessageLike = {
  chatId: string;
  sender: string;
  senderName?: string | null;
  text?: string | null;
  date?: Date | string | null;
  isFromMe?: boolean;
};

export class IMessageAdapter implements MessagePlatformAdapter {
  private readonly sdk: IMessageSDK;

  constructor(logLevel: LogLevel) {
    this.sdk = new IMessageSDK({
      debug: logLevel === "debug",
    });
  }

  async startWatching(handlers: {
    onMessage: (message: IncomingMessage) => Promise<void>;
    onError: (error: Error) => void;
  }): Promise<void> {
    await this.sdk.startWatching({
      onMessage: async (message: PhotonMessageLike) => {
        if (message.isFromMe) {
          return;
        }

        await handlers.onMessage(toIncomingMessage(message));
      },
      onError: (error: unknown) => {
        handlers.onError(toError(error));
      },
    });
  }

  async sendReply(chatId: string, reply: BotReply): Promise<void> {
    if (reply.imagePaths.length > 0) {
      await this.sdk.send(chatId, {
        text: reply.text,
        images: reply.imagePaths,
      });
      return;
    }

    await this.sdk.send(chatId, reply.text);
  }

  async close(): Promise<void> {
    this.sdk.stopWatching();
    try {
      await this.sdk.close();
    } catch (error) {
      console.warn(
        "[photon-bot] Ignore iMessage close error:",
        error instanceof Error ? error.message : String(error),
      );
    }
  }
}

function toIncomingMessage(message: PhotonMessageLike): IncomingMessage {
  const incoming: IncomingMessage = {
    chatId: message.chatId,
    userId: message.sender,
    text: typeof message.text === "string" ? message.text : "",
    receivedAt:
      message.date instanceof Date
        ? message.date.toISOString()
        : typeof message.date === "string"
          ? new Date(message.date).toISOString()
          : new Date().toISOString(),
  };

  if (typeof message.senderName === "string" && message.senderName.trim()) {
    incoming.senderName = message.senderName;
  }

  return incoming;
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}
