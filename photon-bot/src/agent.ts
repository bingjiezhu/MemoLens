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
      text: "Send a photo search description, for example: beach sunset last summer.",
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
        ? "I sent the first two original images."
        : "The previous results are still available, but the original image paths could not be resolved.",
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
    return `${session.lastQueryText}; additional requirement: ${text}`;
  }
  return text;
}

function isSessionDependentFollowUp(text: string): boolean {
  return isNextBatchRequest(text) || isOriginalImageRequest(text) || isRefinementFollowUp(text);
}

function isNextBatchRequest(text: string): boolean {
  return normalizeText(text) === "morelikethis" || normalizeText(text) === "anotherbatch";
}

function isOriginalImageRequest(text: string): boolean {
  const normalized = normalizeText(text);
  return normalized.includes("sendfirsttwooriginals") || normalized.includes("sendfirst2originals");
}

function isRefinementFollowUp(text: string): boolean {
  return /^(keep only|less|more|add|remove|without|with)\b/i.test(text.trim());
}

function normalizeText(text: string): string {
  return text.replace(/[\s!?.,]/g, "").toLowerCase();
}
