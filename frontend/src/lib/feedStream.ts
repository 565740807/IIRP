import type { components } from "../generated/api";

/**
 * The home waterfall's list operations, kept pure so tests run them directly.
 *
 * The server keeps a reading session (watermark): pages read from it never
 * shift, and anything published later is only counted as an update. Applying
 * an update reads the delta between two watermarks; changed company/date
 * groups move to where their sort key puts them, new ones drop in from the
 * top, removed ones leave. Loading older pages only appends.
 */
export type FeedGroup = components["schemas"]["FeedGroup"];
export type FeedOrder = "accepted" | "transaction";

export type FeedStream = {
  /** Session whose cursor pages older groups. */
  session: string;
  /** Newest watermark the list reflects; updates are counted from here. */
  latest: string;
  order: FeedOrder;
  groups: FeedGroup[];
  nextCursor: string | null;
  /** When the list was last read or merged (ISO instant). */
  asOf: string;
  /** Groups that arrived through an update: when, and whether they were already shown. */
  fresh: Record<string, { at: number; updated: boolean }>;
  /** Groups an update removed; older pages of the base session must not bring them back. */
  dropped: string[];
};

/** Sort key: SEC acceptance instant, or the latest (non-anomalous) trade date. */
export function sortKey(group: FeedGroup, order: FeedOrder): number {
  if (order === "transaction") {
    const day = group.transaction_dates.at(-1);
    return day ? Date.parse(`${day}T00:00:00Z`) : -Infinity;
  }
  const accepted = group.accepted_at ? Date.parse(group.accepted_at) : NaN;
  return Number.isFinite(accepted) ? accepted : -Infinity;
}

function newestFirst(order: FeedOrder) {
  return (a: FeedGroup, b: FeedGroup) => sortKey(b, order) - sortKey(a, order);
}

/** Merge one applied delta; returns the list and the ids that were not shown before. */
export function applyDelta(
  stream: FeedStream,
  incoming: FeedGroup[],
  removed: string[],
  target: string,
  now = Date.now(),
): { stream: FeedStream; added: string[] } {
  const replaced = new Set([...incoming.map((group) => group.id), ...removed]);
  const shown = new Set(stream.groups.map((group) => group.id));
  const kept = stream.groups.filter((group) => !replaced.has(group.id));
  // Incoming groups are placed by their key among the kept ones, newest first.
  const ordered = [...incoming].sort(newestFirst(stream.order));
  const groups = [...kept];
  for (const group of ordered) {
    const key = sortKey(group, stream.order);
    const index = groups.findIndex((other) => sortKey(other, stream.order) < key);
    groups.splice(index < 0 ? groups.length : index, 0, group);
  }
  const added = incoming.filter((group) => !shown.has(group.id)).map((group) => group.id);
  const fresh = { ...stream.fresh };
  for (const group of incoming) fresh[group.id] = { at: now, updated: shown.has(group.id) };
  return {
    stream: {
      ...stream, groups, latest: target, asOf: new Date(now).toISOString(), fresh,
      dropped: [...new Set([...stream.dropped, ...removed])].filter((id) => !incoming.some((group) => group.id === id)),
    },
    added,
  };
}

/** Append an older page from the same session; groups already shown stay as they are. */
export function appendPage(stream: FeedStream, page: FeedGroup[], nextCursor: string | null): FeedStream {
  const shown = new Set([...stream.groups.map((group) => group.id), ...stream.dropped]);
  return {
    ...stream,
    groups: [...stream.groups, ...page.filter((group) => !shown.has(group.id))],
    nextCursor,
  };
}

/** "New"/"Updated" marks fade after a while; the list itself does not change. */
export function freshMark(stream: FeedStream, id: string, now = Date.now(), seconds = 12) {
  const mark = stream.fresh[id];
  if (!mark || now - mark.at >= seconds * 1000) return null;
  return mark.updated ? "updated" : "new";
}

/**
 * Whether new groups may be merged right away: only when the reader is at the
 * top and not in the middle of something (text selected, a card expanded is
 * fine because cards keep their identity). Otherwise a "new trades" pill
 * waits for the reader.
 */
export function canApplyInPlace(scrollY: number, selecting: boolean, threshold = 80) {
  return scrollY <= threshold && !selecting;
}
