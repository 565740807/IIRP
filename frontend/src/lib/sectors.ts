import { useEffect, useRef, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { useStockQuotes } from "@/lib/insiderOverview";
import { usePageVisible, useRefreshIntervals } from "@/lib/refresh";
import type { ChartRange, Interval, Period } from "./sectorState";

export * from "./sectorState";

export type SectorPerformance = Schemas["SectorPerformance"];
export type SectorItem = Schemas["SectorItem"];
export type SectorChange = Schemas["SectorChange"];
export type SectorCandles = Schemas["SectorCandlesOutput"];

/** Response field of each period code. */
export const PERIOD_FIELD = { today: "today", "1w": "week", "1m": "month", "3m": "quarter", ytd: "ytd" } as const;

export function periodChange(item: SectorItem, period: Period): SectorChange {
  return item.periods[PERIOD_FIELD[period]];
}

export function periodWeight(item: SectorItem, period: Period) {
  return item.weights[PERIOD_FIELD[period]] ?? null;
}

/**
 * Asks once for the ETFs' daily prices while some are missing or older than
 * the last close, then again every ensure interval until none are; the
 * server answers repeated asks with the same fetch.
 */
function useSectorPrices(pending: readonly string[], foreground: boolean) {
  const visible = usePageVisible();
  const intervals = useRefreshIntervals();
  const queryClient = useQueryClient();
  const active = useRef(false);
  const [error, setError] = useState<Error | null>(null);
  const due = pending.length > 0;
  useEffect(() => {
    if (!visible || !due) return;
    const request = () => {
      if (active.current) return;
      active.current = true;
      void unwrap(client.POST("/api/v1/sectors/prices", { body: { foreground } }))
        .then(() => {
          setError(null);
          void queryClient.invalidateQueries({ queryKey: ["sector-candles"] });
        })
        .catch((reason: Error) => setError(reason))
        .finally(() => { active.current = false; });
    };
    request();
    const timer = window.setInterval(request, intervals.ensure_seconds * 1000);
    return () => window.clearInterval(timer);
  }, [visible, due, foreground, intervals.ensure_seconds, queryClient]);
  return error;
}

/**
 * Sector changes from saved quotes and the daily cache (local reads only),
 * re-read every home poll interval while visible. Opening it asks for the
 * quotes (one merged request) and, when missing, the ETFs' daily prices.
 */
export function useSectors({ foreground = false }: { foreground?: boolean } = {}) {
  const visible = usePageVisible();
  const intervals = useRefreshIntervals();
  const query = useQuery({
    queryKey: ["sectors"],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/sectors", { signal })),
    refetchInterval: visible ? intervals.home_poll_seconds * 1000 : false,
    placeholderData: keepPreviousData,
  });
  const quotes = useStockQuotes(query.data?.quote_symbols ?? [], [["sectors"]]);
  const pricesError = useSectorPrices(query.data?.prices_pending ?? [], foreground);
  return { query, error: query.error ?? quotes.error ?? pricesError };
}

/** One shared range of candles for every sector, aggregated by the backend. */
export function useSectorCandles(range: ChartRange, interval: Interval, enabled: boolean) {
  return useQuery({
    queryKey: ["sector-candles", range, interval],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/sectors/candles", { params: { query: { range, interval } }, signal })),
    enabled,
    placeholderData: keepPreviousData,
  });
}
