import { useEffect, useMemo } from "react";
import { queryOptions, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { DEFAULT_N, parseN, saveN, savedN } from "@/lib/insiderPrefs";

/**
 * Before/after-n changes of insider trades, computed by the server from
 * the 24-hour price cache. Tickers without prices are fetched once (one
 * request per ticker); the windows are read again while a fetch runs.
 */
export type WindowItem = Schemas["WindowItem"];
export type TickerPrices = Schemas["TickerPrices"];
export type TradeKey = { ticker: string | null | undefined; date: string | null | undefined; issuer_id?: string | null };

export const insiderSettingsQuery = queryOptions({
  queryKey: ["insider-settings"],
  queryFn: () => unwrap(client.GET("/api/v1/insider/settings")),
  staleTime: Infinity,
});

/** n from the link, else the remembered choice, else the configured default. */
export function useInsiderN() {
  const [params, setParams] = useSearchParams();
  const settings = useQuery(insiderSettingsQuery).data?.window_sessions;
  const defaultN = settings?.default ?? DEFAULT_N;
  const max = settings?.max ?? 60;
  const n = parseN(params.get("n"), max) ?? savedN() ?? defaultN;
  const setN = (value: number | null) => {
    saveN(value === defaultN ? null : value);
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      if (value === null || value === defaultN) next.delete("n");
      else next.set("n", String(value));
      return next;
    }, { replace: true });
  };
  return { n, setN, defaultN, min: settings?.min ?? 1, max, presets: settings?.presets ?? [3, 5, 10, 20] };
}

function keyOf(item: TradeKey) {
  return `${(item.ticker ?? "").toUpperCase()}|${(item.date ?? "").slice(0, 10)}`;
}

/** Price requests made in this tab (ticker|n|start), so scrolling back does not ask again. */
const asked = new Set<string>();
/** When each ticker was last asked for; its windows are re-read while the fetch gets going. */
const askedAt = new Map<string, number>();

function waiting(tickers: Record<string, TickerPrices> | undefined) {
  return Object.entries(tickers ?? {}).some(([symbol, state]) =>
    state.status === "fetching" || (state.status === "not_fetched" && Date.now() - (askedAt.get(symbol) ?? 0) < 60_000));
}

/** ``foreground``: the reader opened this page (company, person), so its prices go first. */
export function useTradeWindows(items: readonly TradeKey[], n: number, enabled = true, foreground = false) {
  const queryClient = useQueryClient();
  const keys = useMemo(() => {
    const unique = new Map<string, TradeKey>();
    for (const item of items) if (item.ticker && item.date) unique.set(keyOf(item), item);
    return [...unique.values()].sort((a, b) => keyOf(a).localeCompare(keyOf(b)));
  }, [items]);
  const param = keys.map((item) => `${item.ticker}|${item.date!.slice(0, 10)}|${item.issuer_id ?? ""}`).join(",");
  const query = useQuery({
    queryKey: ["insider-windows", n, param],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/insider/windows", { params: { query: { items: param, n } }, signal })),
    enabled: enabled && keys.length > 0,
    placeholderData: (previous) => previous,
    refetchInterval: (state) => (enabled && waiting(state.state.data?.tickers) ? 3000 : false),
  });

  // Ask once for prices of tickers that have none yet; the server fetches each ticker once.
  const tickers = query.data?.tickers;
  useEffect(() => {
    if (!enabled || !tickers) return;
    const due = Object.entries(tickers)
      .filter(([symbol, state]) => state.status === "not_fetched" && !asked.has(`${symbol}|${n}|${state.needed_start}`))
      .map(([symbol, state]) => {
        asked.add(`${symbol}|${n}|${state.needed_start}`);
        askedAt.set(symbol, Date.now());
        return symbol;
      });
    if (!due.length) return;
    const wanted = new Set(due);
    const body = keys.filter((item) => wanted.has((item.ticker ?? "").toUpperCase().replace(".", "-")))
      .slice(0, 500)
      .map((item) => ({ ticker: item.ticker!, date: item.date!.slice(0, 10), issuer_id: item.issuer_id ?? null }));
    if (!body.length) return;
    void unwrap(client.POST("/api/v1/insider/prices", { body: { n, items: body, foreground } }))
      .then(() => queryClient.invalidateQueries({ queryKey: ["insider-windows", n] }))
      .catch(() => {
        /* The cells keep their "no prices" state; opening the page again asks again. */
        for (const symbol of due) asked.delete(`${symbol}|${n}|${tickers[symbol]?.needed_start}`);
      });
  }, [tickers, n, keys, queryClient, foreground, enabled]);

  const byKey = useMemo(() => {
    const map = new Map<string, WindowItem>();
    for (const item of query.data?.items ?? []) map.set(keyOf(item), item);
    return map;
  }, [query.data]);
  return {
    get: (item: TradeKey) => byKey.get(keyOf(item)),
    ticker: (symbol: string | null | undefined) => (symbol ? query.data?.tickers[symbol.toUpperCase().replace(".", "-")] : undefined),
    loading: query.isPending && enabled && keys.length > 0,
    data: query.data,
  };
}

export type Windows = ReturnType<typeof useTradeWindows>;
