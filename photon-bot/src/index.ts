import { BackendClient } from "./backendClient.js";
import { createAgent } from "./agent.js";
import { loadConfig } from "./config.js";
import { DiscordAdapter } from "./discord.js";
import { SessionStore } from "./sessionStore.js";
import type { BotReply, IncomingMessage } from "./types.js";

export async function startBot(): Promise<void> {
  const config = loadConfig();
  const backendClient = new BackendClient(config);
  const sessionStore = new SessionStore(config.sessionTtlMinutes);
  const activeChats = new Set<string>();
  const agent = createAgent({
    config,
    backendClient,
    sessionStore,
  });
  const adapter = new DiscordAdapter(config);

  registerShutdown(adapter);

  log(config.logLevel, "info", "MemoLens Discord bot starting.", {
    backendBaseUrl: config.backendBaseUrl,
    imageLibraryDir: config.imageLibraryDir,
    dbPath: config.dbPath,
    backendSendPathOverrides: config.backendSendPathOverrides,
    discordSendImageWidth: config.discordSendImageWidth,
    backendRequestTimeoutMs: config.backendRequestTimeoutMs,
    allowedChannelCount: config.discordAllowedChannelIds.length,
  });

  await adapter.startWatching({
    onMessage: async (message: IncomingMessage) => {
      log(config.logLevel, "info", "Incoming message received.", {
        chatId: message.chatId,
        senderName: message.senderName ?? null,
      });

      if (activeChats.has(message.chatId)) {
        await adapter.sendReply(message.chatId, {
          text: "I am still working on the previous request in this channel. Give me a moment, then try again.",
          imagePaths: [],
        });
        return;
      }

      activeChats.add(message.chatId);
      let reply: BotReply;
      try {
        reply = await agent.handleIncomingMessage(message);
      } catch (error) {
        log(config.logLevel, "error", "Agent handling failed.", {
          error: error instanceof Error ? error.message : String(error),
        });
        const errorMessage = error instanceof Error ? error.message : String(error);
        reply = {
          text: errorMessage.includes("timed out")
            ? "The backend took too long to finish this query. Please try again in a moment."
            : "I hit an error while processing that request. Please try again in a moment.",
          imagePaths: [],
        };
      } finally {
        activeChats.delete(message.chatId);
      }

      try {
        await adapter.sendReply(message.chatId, reply);
      } catch (error) {
        log(config.logLevel, "error", "Sending reply failed.", {
          error: error instanceof Error ? error.message : String(error),
          chatId: message.chatId,
        });
      }
    },
    onError: (error) => {
      log(config.logLevel, "error", "Discord watcher failed.", {
        error: error.message,
      });
    },
  });

  log(config.logLevel, "info", "Discord bot is running.", null);
}

function registerShutdown(adapter: { close(): Promise<void> }): void {
  const shutdown = async (signal: NodeJS.Signals) => {
    try {
      await adapter.close();
    } finally {
      process.exit(signal === "SIGINT" ? 130 : 143);
    }
  };

  process.once("SIGINT", () => {
    void shutdown("SIGINT");
  });
  process.once("SIGTERM", () => {
    void shutdown("SIGTERM");
  });
}

function log(
  configuredLevel: "debug" | "info" | "warn" | "error",
  level: "debug" | "info" | "warn" | "error",
  message: string,
  meta: Record<string, unknown> | null,
): void {
  const priority = { debug: 10, info: 20, warn: 30, error: 40 };
  if (priority[level] < priority[configuredLevel]) {
    return;
  }

  const prefix = `[${new Date().toISOString()}] ${level.toUpperCase()}`;
  if (meta) {
    console.log(prefix, message, meta);
    return;
  }
  console.log(prefix, message);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  startBot().catch((error) => {
    const message = error instanceof Error ? error.message : String(error);
    if (message.includes("Used disallowed intents")) {
      console.error(
        "Fatal startup error: Message Content Intent is not enabled. In Discord Developer Portal, open Bot -> Privileged Gateway Intents and turn on Message Content Intent, then restart `npm run dev`.",
      );
      process.exit(1);
    }
    console.error("Fatal startup error:", error);
    process.exit(1);
  });
}
