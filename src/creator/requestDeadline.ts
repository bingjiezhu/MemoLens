export function requestDeadline(
  parent: AbortSignal | undefined,
  timeoutMs = 10_000,
): { signal: AbortSignal; cleanup(): void } {
  const controller = new AbortController();
  const forwardAbort = () => controller.abort(parent?.reason);
  if (parent?.aborted) controller.abort(parent.reason);
  else parent?.addEventListener("abort", forwardAbort, { once: true });
  const timeoutId = window.setTimeout(() => {
    controller.abort(new DOMException("Request timed out", "TimeoutError"));
  }, timeoutMs);
  return {
    signal: controller.signal,
    cleanup: () => {
      window.clearTimeout(timeoutId);
      parent?.removeEventListener("abort", forwardAbort);
    },
  };
}
