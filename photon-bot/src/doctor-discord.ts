import { Client, Events, GatewayIntentBits } from "discord.js";

import { loadConfig } from "./config.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const client = new Client({
    intents: [GatewayIntentBits.Guilds],
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
  console.log(
    `Allowed channels: ${
      config.discordAllowedChannelIds.length > 0
        ? config.discordAllowedChannelIds.join(", ")
        : "none configured"
    }`,
  );
  console.log("");
  console.log("Next step: run `npm run dev` in photon-bot.");

  client.destroy();
}

void main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
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
