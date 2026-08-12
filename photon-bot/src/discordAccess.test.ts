import assert from "node:assert/strict";
import test from "node:test";

import {
  shouldHandleDiscordMessage,
  type DiscordAccessPolicy,
  type DiscordMessageAccessInput,
} from "./discordAccess.js";

const baseMessage: DiscordMessageAccessInput = {
  authorId: "100000000000000001",
  channelId: "200000000000000001",
  isAuthorBot: false,
  isSystemMessage: false,
  isDirectMessage: false,
  mentionsBot: false,
};

const policy: DiscordAccessPolicy = {
  allowedUserIds: ["100000000000000001"],
  allowedChannelIds: ["200000000000000001"],
};

test("fails closed when the user allowlist is empty", () => {
  assert.equal(
    shouldHandleDiscordMessage(
      { ...baseMessage, isDirectMessage: true },
      { allowedUserIds: [], allowedChannelIds: [baseMessage.channelId] },
    ),
    false,
  );
});

test("allows an allowlisted user in a DM without a channel allowlist", () => {
  assert.equal(
    shouldHandleDiscordMessage(
      { ...baseMessage, isDirectMessage: true },
      { allowedUserIds: policy.allowedUserIds, allowedChannelIds: [] },
    ),
    true,
  );
});

test("rejects an unlisted user in a DM", () => {
  assert.equal(
    shouldHandleDiscordMessage(
      { ...baseMessage, authorId: "100000000000000002", isDirectMessage: true },
      policy,
    ),
    false,
  );
});

test("allows a guild message only when both user and channel are allowlisted", () => {
  assert.equal(shouldHandleDiscordMessage(baseMessage, policy), true);
  assert.equal(
    shouldHandleDiscordMessage(
      { ...baseMessage, authorId: "100000000000000002" },
      policy,
    ),
    false,
  );
  assert.equal(
    shouldHandleDiscordMessage(
      { ...baseMessage, channelId: "200000000000000002" },
      policy,
    ),
    false,
  );
});

test("a bot mention never bypasses the guild channel allowlist", () => {
  assert.equal(
    shouldHandleDiscordMessage(
      {
        ...baseMessage,
        channelId: "200000000000000002",
        mentionsBot: true,
      },
      policy,
    ),
    false,
  );
  assert.equal(
    shouldHandleDiscordMessage(
      {
        ...baseMessage,
        authorId: "100000000000000002",
        mentionsBot: true,
      },
      policy,
    ),
    false,
  );
});

test("rejects bot-authored and system messages", () => {
  assert.equal(
    shouldHandleDiscordMessage({ ...baseMessage, isAuthorBot: true }, policy),
    false,
  );
  assert.equal(
    shouldHandleDiscordMessage({ ...baseMessage, isSystemMessage: true }, policy),
    false,
  );
});
