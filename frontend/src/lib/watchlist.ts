import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { tickerKey } from "@/lib/tickerMarks";

/**
 * The watched stock codes, kept on the server (one list for every browser).
 * Codes compare as everywhere else (tickerKey: BRK.B is BRK-B). Every page reads
 * the one list query, so marks beside many codes cost a single request.
 */
export type Watchlist = Schemas["WatchlistOutput"];
export type WatchlistEntry = Schemas["WatchlistEntry"];

export const WATCHLIST_KEY = ["watchlist"] as const;

export function useWatchlist() {
  return useQuery({
    queryKey: WATCHLIST_KEY,
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/watchlist", { signal })),
    staleTime: 5 * 60_000,
  });
}

/** The watched codes in their compared form. */
export function useWatchedSet(): ReadonlySet<string> {
  const { data } = useWatchlist();
  return useMemo(() => new Set((data?.items ?? []).map((item) => item.ticker)), [data]);
}

export function useIsWatched(ticker: string | null | undefined) {
  const watched = useWatchedSet();
  const key = tickerKey(ticker);
  return !!key && watched.has(key);
}

/** The list with one code added or removed, as the server will keep it (sorted by code). */
export function withCode(list: Watchlist, key: string, watched: boolean): Watchlist {
  const items = list.items.filter((item) => item.ticker !== key);
  if (watched) {
    const added: WatchlistEntry = { ticker: key, name: null, issuer_id: null, local: true, added_at: new Date().toISOString() };
    items.push(list.items.find((item) => item.ticker === key) ?? added);
    items.sort((a, b) => (a.ticker < b.ticker ? -1 : a.ticker > b.ticker ? 1 : 0));
  }
  return { ...list, items };
}

/**
 * Queries that depend on the list (a view filtered to it, its news) carry this
 * element in their key; they are read again once the server has a change.
 */
export const WATCH_DEPENDENT = "watchlist";

export function refreshWatchViews(queryClient: QueryClient) {
  void queryClient.invalidateQueries({
    predicate: (query) => query.queryKey.indexOf(WATCH_DEPENDENT) > 0,
  });
}

/**
 * Add or remove one code. The change shows at once; when the server refuses it,
 * the list returns to what it was and the error is returned for the caller to show.
 */
export function useWatchToggle() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ key, watched }: { key: string; watched: boolean }) =>
      unwrap(watched
        ? client.POST("/api/v1/watchlist", { body: { ticker: key } })
        : client.DELETE("/api/v1/watchlist/{ticker}", { params: { path: { ticker: key } } })),
    onMutate: async ({ key, watched }) => {
      await queryClient.cancelQueries({ queryKey: WATCHLIST_KEY });
      const previous = queryClient.getQueryData<Watchlist>(WATCHLIST_KEY);
      if (previous) queryClient.setQueryData<Watchlist>(WATCHLIST_KEY, withCode(previous, key, watched));
      return { previous };
    },
    onError: (_error, _change, context) => {
      if (context?.previous) queryClient.setQueryData(WATCHLIST_KEY, context.previous);
      void queryClient.invalidateQueries({ queryKey: WATCHLIST_KEY });
    },
    onSuccess: (data) => {
      queryClient.setQueryData(WATCHLIST_KEY, data);
      refreshWatchViews(queryClient);
    },
  });
}

/** The US Eastern calendar day of an instant, as YYYY-MM-DD. */
export function etDay(value: string) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(value));
}
