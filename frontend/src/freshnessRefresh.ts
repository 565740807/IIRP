import type { components } from "./generated/api";

/** Page presence is a request for shared work; the server owns its cadence. */
export type FreshnessRequest = components["schemas"]["FreshnessInput"];
export type RefreshReason = NonNullable<FreshnessRequest["reason"]>;

export function marketPageVisible(pathname: string) {
  return pathname === "/" || pathname.startsWith("/market/");
}

export function freshnessRequest(
  reason: RefreshReason,
  marketVisible: boolean,
): FreshnessRequest {
  return {
    reason,
    sources:
      reason === "visible" && !marketVisible ? ["sec"] : ["sec", "market"],
    market_visible: marketVisible,
    force: reason === "manual",
  };
}

export function newestFreshness<
  T extends { observed_at?: string | null; sources?: Record<string, unknown> },
>(previous: T | undefined, incoming: T): T {
  if (
    previous?.observed_at &&
    incoming.observed_at &&
    Date.parse(previous.observed_at) > Date.parse(incoming.observed_at)
  )
    return previous;
  if (!previous?.sources) return incoming;
  // A SEC-only heartbeat is not evidence that the saved market state disappeared.
  return { ...incoming, sources: { ...previous.sources, ...incoming.sources } };
}

/** Coalesce focus/visibility events and retain a changed page or manual intent. */
export function createFreshnessRefresher<T>(options: {
  visible: () => boolean;
  marketVisible: () => boolean;
  request: (payload: FreshnessRequest) => Promise<T>;
  started: (reason: RefreshReason) => void;
  received: (value: T) => void;
  failed: (error: unknown) => void;
  settled: () => void;
}) {
  let disposed = false;
  let busy = false;
  let active: FreshnessRequest | undefined;
  let pending: RefreshReason | undefined;
  async function ensure(reason: RefreshReason): Promise<void> {
    if (disposed || (!options.visible() && reason !== "manual")) return;
    const payload = freshnessRequest(reason, options.marketVisible());
    if (busy) {
      if (
        (reason === "manual" && active?.reason !== "manual") ||
        active?.market_visible !== payload.market_visible
      )
        pending = pending === "manual" ? pending : reason;
      return;
    }
    busy = true;
    active = payload;
    options.started(reason);
    try {
      const value = await options.request(payload);
      if (!disposed) options.received(value);
    } catch (error) {
      if (!disposed) options.failed(error);
    } finally {
      busy = false;
      if (!disposed) {
        options.settled();
        const next = pending;
        pending = undefined;
        if (next) await ensure(next);
      }
    }
  }
  return {
    ensure,
    dispose: () => {
      disposed = true;
    },
  };
}
