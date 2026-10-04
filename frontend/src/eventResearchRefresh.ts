/** Keep a late refresh from replacing a new draft, route or explicit snapshot. */
export type EventRefreshContext = {
  analysisId: string;
  originId: string;
  navigation: string;
  epoch: number;
  snapshotId: string;
  dirty: boolean;
  ready: boolean;
};
export function sameEventRefreshContext(
  a: EventRefreshContext,
  b: EventRefreshContext,
) {
  return (
    a.analysisId === b.analysisId &&
    a.navigation === b.navigation &&
    a.epoch === b.epoch &&
    a.snapshotId === b.snapshotId &&
    b.ready &&
    !b.dirty
  );
}
export function eventFollowParams(
  params: URLSearchParams,
  analysisId?: string,
) {
  const next = new URLSearchParams(params);
  next.delete("result");
  next.delete("result_ids");
  if (analysisId) next.set("a", analysisId);
  return next;
}
export function createEventResearchRefresher<T>(options: {
  current: () => EventRefreshContext;
  visible: () => boolean;
  clock?: () => number;
  request: (id: string, manual: boolean) => Promise<T>;
  received: (value: T, context: EventRefreshContext) => void;
  failed: (error: unknown) => void;
  started: () => void;
  settled: () => void;
}) {
  let disposed = false;
  let busy = false;
  const checks = new Map<string, number>();
  async function ensure(manual = false) {
    const context = options.current();
    const clock = options.clock ?? Date.now;
    if (
      disposed ||
      busy ||
      !context.ready ||
      !context.analysisId ||
      context.dirty ||
      (!manual &&
        (context.snapshotId ||
          !options.visible() ||
          clock() - (checks.get(context.originId) ?? -Infinity) < 60000))
    )
      return;
    busy = true;
    checks.set(context.originId, clock());
    options.started();
    try {
      const value = await options.request(context.analysisId, manual);
      if (!disposed && sameEventRefreshContext(context, options.current()))
        options.received(value, context);
    } catch (error) {
      if (!disposed && sameEventRefreshContext(context, options.current()))
        options.failed(error);
    } finally {
      busy = false;
      if (!disposed) options.settled();
    }
  }
  return {
    ensure,
    dispose: () => {
      disposed = true;
    },
  };
}
