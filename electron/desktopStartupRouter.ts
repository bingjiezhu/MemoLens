export const LIBRARY_BOOTSTRAP_WAKE_ARG = "--memolens-library-bootstrap";

export type DesktopStartupRoute = "library_bootstrap_broker" | "normal";

export interface DesktopStartupRouteInput {
  argv: readonly string[];
  hasPendingBootstrap: boolean;
}

export interface DesktopStartupOperations extends DesktopStartupRouteInput {
  runBootstrapBroker(): Promise<unknown>;
  registerBusinessIpc(): unknown;
  ensureBackend(): Promise<unknown>;
  createWindow(): unknown;
}

/**
 * Select a broker-only startup without accepting request data in argv.
 *
 * The opaque request remains in the bounded app-state spool.  An executable
 * wake, if one is added later, may carry only the fixed constant below.
 */
export function chooseDesktopStartupRoute(
  input: DesktopStartupRouteInput,
): DesktopStartupRoute {
  if (input.argv.some((argument) => argument.startsWith(`${LIBRARY_BOOTSTRAP_WAKE_ARG}=`))) {
    throw new Error("MemoLens bootstrap wake must not carry a request payload.");
  }
  return input.hasPendingBootstrap || input.argv.includes(LIBRARY_BOOTSTRAP_WAKE_ARG)
    ? "library_bootstrap_broker"
    : "normal";
}

/** Keeps the broker path ahead of normal business IPC/backend/window startup. */
export async function runDesktopStartupRoute(
  operations: DesktopStartupOperations,
): Promise<DesktopStartupRoute> {
  const route = chooseDesktopStartupRoute(operations);
  if (route === "library_bootstrap_broker") {
    const result = await operations.runBootstrapBroker();
    // Only the broker's completed Core commit + active proof + projections
    // permit promotion. Cancellation, expiry, staged and empty turns stay
    // broker-only; exceptions propagate without business startup.
    if (
      result === null || typeof result !== "object"
      || !("status" in result) || result.status !== "library_authority_committed"
    ) return route;
  }
  operations.registerBusinessIpc();
  await operations.ensureBackend();
  operations.createWindow();
  return "normal";
}
