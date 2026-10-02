/**
 * Closed Electron production-action registry.
 *
 * This module is the runtime registration boundary for main-process IPC.  The
 * sandboxed preload cannot import local runtime modules, so it imports only
 * the channel types derived here; the source-discovery test checks preload
 * call sites in both directions against this registry.
 */

const electronProductionSurfaces = [
  { direction: "renderer-to-main", channel: "memolens:approve-and-export-canonical-timeline" },
  { direction: "renderer-to-main", channel: "memolens:commit-library-selection" },
  { direction: "renderer-to-main", channel: "memolens:ensure-backend" },
  { direction: "renderer-to-main", channel: "memolens:get-settings" },
  { direction: "renderer-to-main", channel: "memolens:list-agent-authority" },
  { direction: "renderer-to-main", channel: "memolens:open-in-codex" },
  { direction: "renderer-to-main", channel: "memolens:pause-indexing" },
  { direction: "renderer-to-main", channel: "memolens:pick-image-folder" },
  { direction: "renderer-to-main", channel: "memolens:request-decision-authority-review" },
  { direction: "renderer-to-main", channel: "memolens:resume-indexing" },
  { direction: "renderer-to-main", channel: "memolens:review-agent-pairing" },
  { direction: "renderer-to-main", channel: "memolens:revoke-agent-capability" },
  { direction: "renderer-to-main", channel: "memolens:save-settings" },
  { direction: "renderer-to-main", channel: "memolens:save-video-artifact" },
  { direction: "renderer-to-main", channel: "memolens:start-indexing" },
  { direction: "main-to-renderer", channel: "memolens:indexing-progress" },
] as const;

for (const entry of electronProductionSurfaces) Object.freeze(entry);
export const ELECTRON_PRODUCTION_SURFACES = Object.freeze(electronProductionSurfaces);

export type ElectronProductionSurface = (typeof ELECTRON_PRODUCTION_SURFACES)[number];
export type ElectronInvokeChannel = Extract<
  ElectronProductionSurface,
  { direction: "renderer-to-main" }
>["channel"];
export type ElectronOutboundEventChannel = Extract<
  ElectronProductionSurface,
  { direction: "main-to-renderer" }
>["channel"];

export const ELECTRON_INVOKE_CHANNELS: readonly ElectronInvokeChannel[] = Object.freeze(
  ELECTRON_PRODUCTION_SURFACES
    .filter((entry) => entry.direction === "renderer-to-main")
    .map((entry) => entry.channel),
);

export const ELECTRON_OUTBOUND_EVENT_CHANNELS: readonly ElectronOutboundEventChannel[] =
  Object.freeze(
    ELECTRON_PRODUCTION_SURFACES
      .filter((entry) => entry.direction === "main-to-renderer")
      .map((entry) => entry.channel),
  );

export const ELECTRON_PRODUCTION_ACTION_IDS: readonly string[] = Object.freeze(
  ELECTRON_PRODUCTION_SURFACES.map((entry) => (
    entry.direction === "renderer-to-main"
      ? `electron_ipc.${entry.channel}`
      : `electron_ipc.event:${entry.channel}`
  )),
);

const invokeChannels = new Set<string>(ELECTRON_INVOKE_CHANNELS);
const outboundEventChannels = new Set<string>(ELECTRON_OUTBOUND_EVENT_CHANNELS);

function assertRegistryIsClosed(): void {
  const channels = ELECTRON_PRODUCTION_SURFACES.map((entry) => entry.channel);
  if (channels.length !== new Set(channels).size) {
    throw new Error("Electron production surface registry contains a duplicate channel.");
  }
  if (ELECTRON_PRODUCTION_ACTION_IDS.length !== new Set(ELECTRON_PRODUCTION_ACTION_IDS).size) {
    throw new Error("Electron production surface registry contains a duplicate action ID.");
  }
}

assertRegistryIsClosed();

function assertInvokeChannel(channel: string): asserts channel is ElectronInvokeChannel {
  if (!invokeChannels.has(channel)) {
    throw new Error(`Unknown Electron invoke channel rejected: ${channel}`);
  }
}

function assertOutboundEventChannel(
  channel: string,
): asserts channel is ElectronOutboundEventChannel {
  if (!outboundEventChannels.has(channel)) {
    throw new Error(`Unknown Electron outbound event channel rejected: ${channel}`);
  }
}

export type ProductionIpcHandler = (
  event: Electron.IpcMainInvokeEvent,
  ...args: any[]
) => Promise<any> | any;

export interface ProductionIpcHandlerTarget {
  handle(channel: string, listener: ProductionIpcHandler): void;
}

export interface ProductionIpcEventSender {
  send(channel: string, ...args: any[]): void;
}

export class ProductionIpcHandlerRegistry {
  private readonly registeredChannels = new Set<ElectronInvokeChannel>();

  constructor(private readonly target: ProductionIpcHandlerTarget) {}

  handle(channel: ElectronInvokeChannel, listener: ProductionIpcHandler): void {
    assertInvokeChannel(channel);
    if (this.registeredChannels.has(channel)) {
      throw new Error(`Duplicate Electron invoke handler rejected: ${channel}`);
    }
    this.target.handle(channel, listener);
    this.registeredChannels.add(channel);
  }

  assertComplete(): void {
    const missing = ELECTRON_INVOKE_CHANNELS.filter(
      (channel) => !this.registeredChannels.has(channel),
    );
    if (missing.length > 0) {
      throw new Error(`Electron invoke handlers are incomplete: ${missing.join(", ")}`);
    }
  }
}

export function sendProductionIpcEvent(
  sender: ProductionIpcEventSender,
  channel: ElectronOutboundEventChannel,
  ...args: any[]
): void {
  assertOutboundEventChannel(channel);
  sender.send(channel, ...args);
}
