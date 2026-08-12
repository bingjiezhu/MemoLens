import { Client, Events, GatewayIntentBits } from "discord.js";

import { loadConfig } from "./config.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const client = new Client({
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMessages,
      GatewayIntentBits.DirectMessages,
      GatewayIntentBits.MessageContent,
    ],
  });

  await new Promise<void>((resolve, reject) => {
    client.once(Events.ClientReady, () => resolve());
    client.once(Events.Error, (error) => reject(error));
    void client.login(config.discordBotToken).catch(reject);
  });

  const application = await client.application?.fetch();

  console.log("MemoLens Discord Doctor");
  console.log(`Bot user: ${client.user?.tag ?? "unknown"} (${client.user?.id ?? "unknown"})`);
  console.log(`Application: ${application?.name ?? "unknown"}`);
  console.log(`Guilds visible: ${client.guilds.cache.size}`);
  console.log(`Allowed users (required): ${config.discordAllowedUserIds.join(", ")}`);
  console.log(
    `Allowed guild channels: ${
      config.discordAllowedChannelIds.length > 0
        ? config.discordAllowedChannelIds.join(", ")
        : "none configured (all guild messages are disabled)"
    }`,
  );
  console.log("");
  console.log("Access policy:");
  console.log("- DMs require an allowed user ID.");
  console.log("- Guild messages require both an allowed user ID and an allowed channel ID.");
  console.log("- Mentioning the bot never bypasses either allowlist.");
  console.log("");
  console.log("Privacy note: image replies upload copies of matching local photos to Discord.");
  console.log("");
  console.log("Next step: run `npm run dev` in photon-bot.");

  client.destroy();
}

void main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  if (message.includes("DISCORD_ALLOWED_USER_IDS")) {
    console.error(`Discord doctor failed: ${message}`);
    console.error(
      "Set DISCORD_ALLOWED_USER_IDS to a comma-separated list of trusted Discord user IDs. The bot will not start without it.",
    );
    process.exit(1);
  }
  if (message.includes("Used disallowed intents")) {
    console.error(
      "Discord doctor failed: Message Content Intent is not enabled. In Discord Developer Portal, open Bot -> Privileged Gateway Intents and turn on Message Content Intent, then save and rerun.",
    );
    process.exit(1);
  }
  console.error(
    "Discord doctor failed:",
    message,
  );
  process.exit(1);
});
