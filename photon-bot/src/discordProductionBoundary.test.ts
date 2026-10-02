import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import type { Client, Message } from "discord.js";
import { Events } from "discord.js";
import sharp from "sharp";

import { createAgent } from "./agent.js";
import type { BackendClient } from "./backendClient.js";
import type { BotConfig } from "./config.js";
import { DiscordAdapter } from "./discord.js";
import { resolveImagePath } from "./imageResolver.js";
import { PhotonNetworkPolicyError } from "./networkPolicy.js";
import { SessionStore } from "./sessionStore.js";
import type { IncomingMessage, RetrievalResponse } from "./types.js";

type EventHandler = (...arguments_: unknown[]) => unknown;

class FakeDiscordClient {
  readonly user = { id: "fixture-bot" };
  readonly handlers = new Map<string, EventHandler[]>();
  readonly sentPayloads: unknown[] = [];
  destroyCount = 0;
  fetchCount = 0;
  loginCount = 0;
  sendCount = 0;

  readonly channels: {
    fetch: (chatId: string) => Promise<{
      isTextBased: () => boolean;
      send: (payload: unknown) => Promise<void>;
    }>;
  };

  constructor() {
    this.channels = {
      fetch: async (_chatId: string) => {
        this.fetchCount += 1;
        return {
          isTextBased: () => true,
          send: async (payload: unknown) => {
            this.sendCount += 1;
            this.sentPayloads.push(payload);
          },
        };
      },
    };
  }

  on(event: string, handler: EventHandler): this {
    const handlers = this.handlers.get(event) ?? [];
    handlers.push(handler);
    this.handlers.set(event, handlers);
    return this;
  }

  async login(_token: string): Promise<string> {
    this.loginCount += 1;
    return "fixture-login";
  }

  destroy(): void {
    this.destroyCount += 1;
  }

  async emitMessage(message: Message): Promise<void> {
    for (const handler of this.handlers.get(Events.MessageCreate) ?? []) {
      await handler(message);
    }
  }
}

function configFor(libraryRoot: string, overrides: Partial<BotConfig> = {}): BotConfig {
  return {
    backendBaseUrl: "http://127.0.0.1:5519",
    imageLibraryDir: libraryRoot,
    dbPath: null,
    backendSendPathOverrides: false,
    discordSendImageWidth: 32,
    discordBotToken: "fixture-token",
    discordExpectedBotUserId: "fixture-bot",
    discordAllowedUserIds: ["allowed-user"],
    discordAllowedChannelIds: ["allowed-channel"],
    backendRequestTimeoutMs: 1_000,
    defaultTopK: 3,
    defaultReplyImageCount: 2,
    sessionTtlMinutes: 30,
    logLevel: "error",
    requestTimeoutMs: 1_000,
    ...overrides,
  };
}

function adapterFor(config: BotConfig, fake: FakeDiscordClient): DiscordAdapter {
  return new DiscordAdapter(config, {
    client: fake as unknown as Client,
  });
}

function discordMessage(input: {
  authorId?: string;
  channelId?: string;
  content?: string;
  directMessage?: boolean;
} = {}): Message {
  return {
    author: {
      id: input.authorId ?? "allowed-user",
      bot: false,
      globalName: null,
      username: "fixture-user",
    },
    channelId: input.channelId ?? "allowed-channel",
    system: false,
    channel: {
      isDMBased: () => input.directMessage ?? false,
    },
    mentions: {
      users: {
        has: () => false,
      },
    },
    content: input.content ?? "find beach photos",
    member: null,
    createdAt: new Date("2026-08-29T00:00:00.000Z"),
  } as unknown as Message;
}

async function fixtureRoot(t: test.TestContext): Promise<string> {
  const root = await fs.promises.mkdtemp(
    path.join(os.tmpdir(), "memolens-photon-production-boundary-"),
  );
  t.after(async () => {
    await fs.promises.rm(root, { force: true, recursive: true });
  });
  return root;
}

function setNetworkProfile(
  t: test.TestContext,
  profile: "online" | "offline",
): void {
  const previous = process.env.MEMOLENS_NETWORK_PROFILE;
  process.env.MEMOLENS_NETWORK_PROFILE = profile;
  t.after(() => {
    if (previous === undefined) delete process.env.MEMOLENS_NETWORK_PROFILE;
    else process.env.MEMOLENS_NETWORK_PROFILE = previous;
  });
}

async function startAndCapture(
  adapter: DiscordAdapter,
  fake: FakeDiscordClient,
  count: number,
): Promise<IncomingMessage[]> {
  const messages: IncomingMessage[] = [];
  await adapter.startWatching({
    onMessage: async (message) => {
      messages.push(message);
    },
    onError: (error) => {
      throw error;
    },
  });
  for (let index = 0; index < count; index += 1) {
    await fake.emitMessage(
      discordMessage({ content: `fixture request ${index + 1}` }),
    );
  }
  assert.equal(messages.length, count);
  return messages;
}

test("Discord adapter denies empty and wrong user-channel allowlists before downstream work", async (t) => {
  setNetworkProfile(t, "online");
  const root = await fixtureRoot(t);

  const emptyClient = new FakeDiscordClient();
  const emptyAdapter = adapterFor(
    configFor(root, { discordAllowedUserIds: [] }),
    emptyClient,
  );
  let emptyHandlerCount = 0;
  await assert.rejects(
    emptyAdapter.startWatching({
      onMessage: async () => {
        emptyHandlerCount += 1;
      },
      onError: (error) => {
        throw error;
      },
    }),
    /at least one allowed user is required/,
  );
  assert.deepEqual(
    {
      handler: emptyHandlerCount,
      login: emptyClient.loginCount,
      fetch: emptyClient.fetchCount,
      send: emptyClient.sendCount,
    },
    { handler: 0, login: 0, fetch: 0, send: 0 },
  );

  const guardedClient = new FakeDiscordClient();
  const guardedAdapter = adapterFor(configFor(root), guardedClient);
  const downstream = { attachment: 0, query: 0, reply: 0, resolve: 0 };
  await guardedAdapter.startWatching({
    onMessage: async () => {
      downstream.query += 1;
      downstream.resolve += 1;
      downstream.reply += 1;
      downstream.attachment += 1;
    },
    onError: (error) => {
      throw error;
    },
  });
  await guardedClient.emitMessage(
    discordMessage({ authorId: "wrong-user", directMessage: true }),
  );
  await guardedClient.emitMessage(
    discordMessage({ channelId: "wrong-channel" }),
  );

  assert.deepEqual(downstream, { attachment: 0, query: 0, reply: 0, resolve: 0 });
  assert.equal(guardedClient.loginCount, 1);
  assert.equal(guardedClient.fetchCount, 0);
  assert.equal(guardedClient.sendCount, 0);
  await guardedAdapter.close();
});

test("forged Discord worker admission denies query resolve reply and attachments before side effects", async (t) => {
  setNetworkProfile(t, "online");
  const root = await fixtureRoot(t);
  const fake = new FakeDiscordClient();
  const config = configFor(root);
  const adapter = adapterFor(config, fake);
  await adapter.startWatching({
    onMessage: async () => undefined,
    onError: (error) => {
      throw error;
    },
  });

  let queryCount = 0;
  const backend = {
    queryPhotos: async (): Promise<RetrievalResponse> => {
      queryCount += 1;
      return { status: "completed", notes: [], data: [] };
    },
  } as unknown as BackendClient;
  const sessions = new SessionStore(30);
  const agent = createAgent({
    config,
    backendClient: backend,
    sessionStore: sessions,
    requireMessageAdmission: (message) => adapter.requireAdmittedMessage(message),
  });
  const forged: IncomingMessage = {
    chatId: "allowed-channel",
    userId: "allowed-user",
    text: "find beach photos",
    receivedAt: "2026-08-29T00:00:00.000Z",
    admission: Object.freeze({ forged: true }),
  };

  await assert.rejects(agent.handleIncomingMessage(forged), /worker admission denied/);
  sessions.set(forged.chatId, {
    lastQueryText: "fixture",
    lastRelativePaths: ["private-original.jpg"],
    lastResultOffset: 0,
    updatedAt: new Date().toISOString(),
  });
  await assert.rejects(
    agent.handleIncomingMessage({ ...forged, text: "send first two originals" }),
    /worker admission denied/,
  );
  await assert.rejects(
    adapter.sendReply(forged.chatId, { text: "forged", imagePaths: [] }, forged.admission),
    /worker admission denied/,
  );
  await assert.rejects(
    adapter.sendReply(
      forged.chatId,
      { text: "forged", imagePaths: [path.join(root, "private-original.jpg")] },
      forged.admission,
    ),
    /worker admission denied/,
  );

  assert.equal(queryCount, 0);
  assert.equal(fake.fetchCount, 0);
  assert.equal(fake.sendCount, 0);
  await adapter.close();
});

test("Discord admission is user-chat bound and one-shot before scoped side effects", async (t) => {
  setNetworkProfile(t, "online");
  const root = await fixtureRoot(t);
  const imagePath = path.join(root, "scoped-private.png");
  await sharp({
    create: {
      width: 16,
      height: 16,
      channels: 3,
      background: { r: 10, g: 20, b: 30 },
    },
  })
    .png()
    .toFile(imagePath);

  const fake = new FakeDiscordClient();
  const config = configFor(root);
  const adapter = adapterFor(config, fake);
  const admitted = await startAndCapture(adapter, fake, 5);
  let queryCount = 0;
  const backend = {
    queryPhotos: async (): Promise<RetrievalResponse> => {
      queryCount += 1;
      return { status: "completed", notes: [], data: [] };
    },
  } as unknown as BackendClient;
  const sessions = new SessionStore(30);
  sessions.set(admitted[1]!.chatId, {
    lastQueryText: "fixture",
    lastRelativePaths: [path.basename(imagePath)],
    lastResultOffset: 0,
    updatedAt: new Date().toISOString(),
  });
  const agent = createAgent({
    config,
    backendClient: backend,
    sessionStore: sessions,
    requireMessageAdmission: (message) => adapter.requireAdmittedMessage(message),
  });

  await assert.rejects(
    agent.handleIncomingMessage({
      ...admitted[0]!,
      chatId: "different-chat",
    }),
    /worker admission denied/,
  );
  await assert.rejects(
    agent.handleIncomingMessage({
      ...admitted[1]!,
      userId: "different-user",
      text: "send first two originals",
    }),
    /worker admission denied/,
  );
  await assert.rejects(
    adapter.sendReply(
      "different-chat",
      { text: "wrong scope", imagePaths: [] },
      admitted[2]!.admission,
    ),
    /stale or out of scope/,
  );
  await assert.rejects(
    adapter.sendReply(
      "different-chat",
      { text: "wrong scope", imagePaths: [imagePath] },
      admitted[3]!.admission,
    ),
    /stale or out of scope/,
  );
  const admittedReply = await agent.handleIncomingMessage(admitted[4]!);
  assert.deepEqual(admittedReply.imagePaths, []);
  await assert.rejects(
    agent.handleIncomingMessage(admitted[4]!),
    /worker admission denied/,
  );
  assert.equal(queryCount, 1);
  assert.equal(fake.fetchCount, 0);
  assert.equal(fake.sendCount, 0);

  await adapter.sendReply(
    admitted[2]!.chatId,
    { text: "one use", imagePaths: [] },
    admitted[2]!.admission,
  );
  await assert.rejects(
    adapter.sendReply(
      admitted[2]!.chatId,
      { text: "replay", imagePaths: [] },
      admitted[2]!.admission,
    ),
    /stale or out of scope/,
  );
  assert.equal(fake.fetchCount, 1);
  assert.equal(fake.sendCount, 1);
  await adapter.close();
  await assert.rejects(
    agent.handleIncomingMessage(admitted[3]!),
    /worker admission denied/,
  );
  assert.equal(queryCount, 1);
});

test("offline profile denies Discord connect receive reply and attachment egress before login handler fetch or send", async (t) => {
  const root = await fixtureRoot(t);
  setNetworkProfile(t, "offline");

  const connectClient = new FakeDiscordClient();
  const connectAdapter = adapterFor(configFor(root), connectClient);
  await assert.rejects(
    connectAdapter.startWatching({
      onMessage: async () => undefined,
      onError: (error) => {
        throw error;
      },
    }),
    PhotonNetworkPolicyError,
  );
  assert.deepEqual(
    {
      login: connectClient.loginCount,
      fetch: connectClient.fetchCount,
      send: connectClient.sendCount,
    },
    { login: 0, fetch: 0, send: 0 },
  );

  process.env.MEMOLENS_NETWORK_PROFILE = "online";
  const sendClient = new FakeDiscordClient();
  const sendAdapter = adapterFor(configFor(root), sendClient);
  const admitted = await startAndCapture(sendAdapter, sendClient, 2);
  process.env.MEMOLENS_NETWORK_PROFILE = "offline";

  let receiveHandlerCount = 0;
  let receiveError: Error | undefined;
  const receiveClient = new FakeDiscordClient();
  process.env.MEMOLENS_NETWORK_PROFILE = "online";
  const receiveAdapter = adapterFor(configFor(root), receiveClient);
  await receiveAdapter.startWatching({
    onMessage: async () => {
      receiveHandlerCount += 1;
    },
    onError: (error) => {
      receiveError = error;
    },
  });
  process.env.MEMOLENS_NETWORK_PROFILE = "offline";
  await receiveClient.emitMessage(discordMessage());
  assert.equal(receiveHandlerCount, 0);
  assert.ok(receiveError instanceof PhotonNetworkPolicyError);
  assert.equal(receiveClient.fetchCount, 0);
  assert.equal(receiveClient.sendCount, 0);

  await assert.rejects(
    sendAdapter.sendReply(
      admitted[0]!.chatId,
      { text: "offline text", imagePaths: [] },
      admitted[0]!.admission,
    ),
    PhotonNetworkPolicyError,
  );
  await assert.rejects(
    sendAdapter.sendReply(
      admitted[1]!.chatId,
      { text: "offline image", imagePaths: [path.join(root, "private.jpg")] },
      admitted[1]!.admission,
    ),
    PhotonNetworkPolicyError,
  );

  assert.equal(sendClient.loginCount, 1);
  assert.equal(sendClient.fetchCount, 0);
  assert.equal(sendClient.sendCount, 0);
  await receiveAdapter.close();
  await sendAdapter.close();
});

test("Discord external actions reject missing provider credential before login fetch or send", async (t) => {
  setNetworkProfile(t, "online");
  const root = await fixtureRoot(t);

  const connectClient = new FakeDiscordClient();
  const connectAdapter = adapterFor(
    configFor(root, { discordBotToken: "   " }),
    connectClient,
  );
  await assert.rejects(
    connectAdapter.startWatching({
      onMessage: async () => undefined,
      onError: (error) => {
        throw error;
      },
    }),
    /provider credential denied/,
  );
  assert.equal(connectClient.loginCount, 0);

  const config = configFor(root);
  const sendClient = new FakeDiscordClient();
  const sendAdapter = adapterFor(config, sendClient);
  const admitted = await startAndCapture(sendAdapter, sendClient, 2);
  config.discordBotToken = "";

  await assert.rejects(
    sendAdapter.sendReply(
      admitted[0]!.chatId,
      { text: "credentialless", imagePaths: [] },
      admitted[0]!.admission,
    ),
    /provider credential denied/,
  );
  await assert.rejects(
    sendAdapter.sendReply(
      admitted[1]!.chatId,
      { text: "credentialless", imagePaths: [path.join(root, "private.jpg")] },
      admitted[1]!.admission,
    ),
    /provider credential denied/,
  );
  assert.equal(sendClient.fetchCount, 0);
  assert.equal(sendClient.sendCount, 0);
  await sendAdapter.close();
});

test("Discord connect rejects an authenticated account outside the configured bot scope", async (t) => {
  setNetworkProfile(t, "online");
  const root = await fixtureRoot(t);
  const client = new FakeDiscordClient();
  const adapter = adapterFor(
    configFor(root, { discordExpectedBotUserId: "different-bot" }),
    client,
  );
  let handlerCount = 0;

  await assert.rejects(
    adapter.startWatching({
      onMessage: async () => {
        handlerCount += 1;
      },
      onError: (error) => {
        throw error;
      },
    }),
    /account scope denied/,
  );
  await client.emitMessage(discordMessage());
  assert.deepEqual(
    {
      destroy: client.destroyCount,
      fetch: client.fetchCount,
      handler: handlerCount,
      login: client.loginCount,
      send: client.sendCount,
    },
    { destroy: 1, fetch: 0, handler: 0, login: 1, send: 0 },
  );
});

test("Discord attachment egress revalidates source identity and sends only derived bytes", async (t) => {
  setNetworkProfile(t, "online");
  const root = await fixtureRoot(t);
  const validPath = path.join(root, "valid-original.png");
  const swappedPath = path.join(root, "swap-original.png");
  const outsidePath = path.join(path.dirname(root), `${path.basename(root)}-outside.png`);
  t.after(async () => {
    await fs.promises.rm(outsidePath, { force: true });
  });
  await sharp({
    create: {
      width: 64,
      height: 32,
      channels: 4,
      background: { r: 200, g: 50, b: 20, alpha: 0.4 },
    },
  })
    .png()
    .toFile(validPath);
  await fs.promises.copyFile(validPath, swappedPath);
  await fs.promises.copyFile(validPath, outsidePath);
  const originalBytes = await fs.promises.readFile(validPath);
  const previouslyResolvedSwap = resolveImagePath(root, path.basename(swappedPath));

  const fake = new FakeDiscordClient();
  const adapter = adapterFor(configFor(root), fake);
  const admitted = await startAndCapture(adapter, fake, 2);
  await adapter.sendReply(
    admitted[0]!.chatId,
    { text: "derived", imagePaths: [validPath] },
    admitted[0]!.admission,
  );

  assert.equal(fake.fetchCount, 1);
  assert.equal(fake.sendCount, 1);
  const payload = fake.sentPayloads[0] as {
    files: Array<{ attachment: unknown; name: string }>;
  };
  assert.equal(payload.files.length, 1);
  assert.equal(Buffer.isBuffer(payload.files[0]!.attachment), true);
  assert.notDeepEqual(payload.files[0]!.attachment, originalBytes);
  assert.equal(payload.files[0]!.name, "memolens-image-1.jpg");
  assert.equal(JSON.stringify(payload).includes(validPath), false);

  await fs.promises.unlink(swappedPath);
  await fs.promises.symlink(outsidePath, swappedPath);
  await assert.rejects(
    adapter.sendReply(
      admitted[1]!.chatId,
      { text: "must not send", imagePaths: [previouslyResolvedSwap] },
      admitted[1]!.admission,
    ),
    /source must pass identity and derived-artifact validation/,
  );
  assert.equal(fake.fetchCount, 1);
  assert.equal(fake.sendCount, 1);
  await adapter.close();
});

test("admitted resolve-originals rejects traversal before query or attachment egress", async (t) => {
  setNetworkProfile(t, "online");
  const root = await fixtureRoot(t);
  const fake = new FakeDiscordClient();
  const config = configFor(root);
  const adapter = adapterFor(config, fake);
  const [message] = await startAndCapture(adapter, fake, 1);
  let queryCount = 0;
  const backend = {
    queryPhotos: async (): Promise<RetrievalResponse> => {
      queryCount += 1;
      return { status: "completed", notes: [], data: [] };
    },
  } as unknown as BackendClient;
  const sessions = new SessionStore(30);
  sessions.set(message!.chatId, {
    lastQueryText: "fixture",
    lastRelativePaths: ["../outside-private.png"],
    lastResultOffset: 0,
    updatedAt: new Date().toISOString(),
  });
  const agent = createAgent({
    config,
    backendClient: backend,
    sessionStore: sessions,
    requireMessageAdmission: (incoming) => adapter.requireAdmittedMessage(incoming),
  });

  const reply = await agent.handleIncomingMessage({
    ...message!,
    text: "send first two originals",
  });
  assert.deepEqual(reply.imagePaths, []);
  assert.match(reply.text, /could not be resolved safely/);
  assert.equal(queryCount, 0);
  assert.equal(fake.fetchCount, 0);
  assert.equal(fake.sendCount, 0);
  await adapter.close();
});
