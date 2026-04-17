import type { BotConfig } from "./config.js";
import { BackendClient } from "./backendClient.js";
import { formatNextBatchReply, formatNoSessionReply, formatReply } from "./formatReply.js";
import { resolveImageBatch } from "./imageResolver.js";
import { SessionStore } from "./sessionStore.js";
import type { BotReply, IncomingMessage, SessionState } from "./types.js";

export type AgentDependencies = {
  config: BotConfig;
  backendClient: BackendClient;
  sessionStore: SessionStore;
};

export function createAgent(dependencies: AgentDependencies) {
  return {
    handleIncomingMessage: (message: IncomingMessage) =>
      handleIncomingMessage(message, dependencies),
  };
}

export async function handleIncomingMessage(
  message: IncomingMessage,
  dependencies: AgentDependencies,
): Promise<BotReply> {
  dependencies.sessionStore.sweep();

  const text = message.text.trim();
  const sessionKey = message.chatId;
  const session = dependencies.sessionStore.get(sessionKey);

  if (!text) {
    return {
      text: "发一句你想找的照片描述，例如：去年夏天海边日落。",
      imagePaths: [],
    };
  }

  if (isSessionDependentFollowUp(text) && !session) {
    return formatNoSessionReply();
  }

  if (isNextBatchRequest(text) && session) {
    return buildNextBatchReply(sessionKey, session, dependencies);
  }

  if (isOriginalImageRequest(text) && session) {
    const batch = resolveImageBatch(
      dependencies.config.imageLibraryDir,
      session.lastRelativePaths.slice(0, 2),
      2,
    );
    return {
      text: batch.imagePaths.length
        ? "我把前两张原图发给你。"
        : "上一轮结果还在，但对应原图没有解析成功。",
      imagePaths: batch.imagePaths,
    };
  }

  const effectiveQuery = buildEffectiveQuery(text, session);
  const result = await dependencies.backendClient.queryPhotos({
    text: effectiveQuery,
    topK: dependencies.config.defaultTopK,
  });

  const relativePaths = result.data.map((item) => item.relative_path);
  const initialBatch = resolveImageBatch(
    dependencies.config.imageLibraryDir,
    relativePaths,
    dependencies.config.defaultReplyImageCount,
  );

  if (result.status === "completed" && relativePaths.length > 0) {
    dependencies.sessionStore.set(sessionKey, {
      lastQueryText: effectiveQuery,
      lastRelativePaths: relativePaths,
      lastResultOffset: initialBatch.consumedCount,
      updatedAt: new Date().toISOString(),
    });
  }

  return formatReply(result, initialBatch.imagePaths);
}

function buildNextBatchReply(
  sessionKey: string,
  session: SessionState,
  dependencies: AgentDependencies,
): BotReply {
  const remainingPaths = session.lastRelativePaths.slice(session.lastResultOffset);
  const batch = resolveImageBatch(
    dependencies.config.imageLibraryDir,
    remainingPaths,
    dependencies.config.defaultReplyImageCount,
  );

  if (batch.imagePaths.length > 0 || batch.consumedCount > 0) {
    dependencies.sessionStore.set(sessionKey, {
      ...session,
      lastResultOffset: session.lastResultOffset + batch.consumedCount,
      updatedAt: new Date().toISOString(),
    });
  }

  return formatNextBatchReply(batch.imagePaths);
}

function buildEffectiveQuery(text: string, session?: SessionState): string {
  if (session && isRefinementFollowUp(text)) {
    return `${session.lastQueryText}；补充要求：${text}`;
  }
  return text;
}

function isSessionDependentFollowUp(text: string): boolean {
  return isNextBatchRequest(text) || isOriginalImageRequest(text) || isRefinementFollowUp(text);
}

function isNextBatchRequest(text: string): boolean {
  return normalizeText(text) === "再来一组";
}

function isOriginalImageRequest(text: string): boolean {
  const normalized = normalizeText(text);
  return normalized.includes("发前两张原图") || normalized.includes("发前2张原图");
}

function isRefinementFollowUp(text: string): boolean {
  return /^(只保留|少一点|要|多一点)/.test(text.trim());
}

function normalizeText(text: string): string {
  return text.replace(/[\s!！?？。,.，]/g, "");
}
