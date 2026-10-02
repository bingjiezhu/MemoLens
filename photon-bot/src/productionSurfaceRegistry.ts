/** Closed production actions for the supported Photon adapters. */

export const PHOTON_PRODUCTION_ACTION_IDS = Object.freeze([
  "photon_bot.discord.connect",
  "photon_bot.discord.query_photos",
  "photon_bot.discord.receive_message",
  "photon_bot.discord.resolve_originals",
  "photon_bot.discord.send_attachments",
  "photon_bot.discord.send_reply",
] as const);

export type PhotonProductionActionId = (typeof PHOTON_PRODUCTION_ACTION_IDS)[number];

const actions = new Set<string>(PHOTON_PRODUCTION_ACTION_IDS);

if (actions.size !== PHOTON_PRODUCTION_ACTION_IDS.length) {
  throw new Error("Photon production surface registry contains duplicate action IDs.");
}

export function requirePhotonProductionAction(
  actionId: PhotonProductionActionId,
): PhotonProductionActionId {
  if (!actions.has(actionId)) {
    throw new Error(`Unknown Photon production action rejected: ${String(actionId)}`);
  }
  return actionId;
}
