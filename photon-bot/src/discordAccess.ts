export type DiscordMessageAccessInput = Readonly<{
  authorId: string;
  channelId: string;
  isAuthorBot: boolean;
  isSystemMessage: boolean;
  isDirectMessage: boolean;
  mentionsBot: boolean;
}>;

export type DiscordAccessPolicy = Readonly<{
  allowedUserIds: readonly string[];
  allowedChannelIds: readonly string[];
}>;

/**
 * Applies the complete inbound Discord access policy without network or client
 * state. A mention is intentionally not an authorization signal.
 */
export function shouldHandleDiscordMessage(
  message: DiscordMessageAccessInput,
  policy: DiscordAccessPolicy,
): boolean {
  if (message.isAuthorBot || message.isSystemMessage) {
    return false;
  }

  if (!policy.allowedUserIds.includes(message.authorId)) {
    return false;
  }

  if (message.isDirectMessage) {
    return true;
  }

  return policy.allowedChannelIds.includes(message.channelId);
}
