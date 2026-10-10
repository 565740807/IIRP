import { useEffect, useRef, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { useStockQuotes } from "@/lib/insiderOverview";
import { usePageVisible, useRefreshIntervals } from "@/lib/refresh";
import type { ChartRange, Interval, Level, Period } from "./sectorState";

export * from "./sectorState";

export type SectorPerformance = Schemas["SectorPerformance"];
export type SectorItem = Schemas["SectorItem"];
export type SectorChange = Schemas["SectorChange"];
export type SectorCandles = Schemas["SectorCandlesOutput"];
export type SectorGroups = Schemas["SectorGroups"];
export type SectorGroupItem = Schemas["SectorGroupItem"];
export type EtfPerformance = Schemas["EtfPerformance"];
export type PriceFetch = Schemas["PriceFetch"];
/** A row that has an ETF and its periods: a sector, a ranked group or a reference row. */
export type PricedRow = { id: string; etf: string; periods: SectorItem["periods"]; weights?: SectorItem["weights"] | null; first_date?: string | null; full_years: number };

/** Response field of each period code. */
export const PERIOD_FIELD = { today: "today", "1w": "week", "1m": "month", "3m": "quarter", ytd: "ytd" } as const;

export function periodChange(item: Pick<PricedRow, "periods">, period: Period): SectorChange {
  return item.periods[PERIOD_FIELD[period]];
}

export function periodWeight(item: PricedRow, period: Period) {
  return item.weights?.[PERIOD_FIELD[period]] ?? null;
}

/** The backend's rank of a group for a period (1 = largest change); null when not ranked. */
export function periodRank(item: SectorGroupItem, period: Period) {
  return item.ranks?.[PERIOD_FIELD[period]] ?? null;
}

/** Groups with a primary ETF, as rows the heatmap and the charts take. */
export function rankedGroups(items: readonly SectorGroupItem[]): (SectorGroupItem & PricedRow)[] {
  return items.filter((item): item is SectorGroupItem & PricedRow => item.etf != null && item.periods != null);
}

/**
 * Asks once for the ETFs' daily prices while some are missing or older than
 * the last close, then again every ensure interval until none are; the
 * server answers repeated asks with the same fetch.
 */
type PriceScope = { level: Level; sector?: string | null };

function useSectorPrices(pending: readonly string[], foreground: boolean, scope: PriceScope = { level: "sector" }) {
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
      void unwrap(client.POST("/api/v1/sectors/prices", { body: { foreground, level: scope.level, sector: scope.sector ?? null } }))
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
  }, [visible, due, foreground, scope.level, scope.sector, intervals.ensure_seconds, queryClient]);
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

/**
 * Industry groups of one sector, or all of them (local reads only). Opening
 * the level asks for its ETFs' quotes and, when missing, their daily prices;
 * nothing else ever asks for the group ETFs.
 */
export function useSectorGroups(sector: string | null, enabled: boolean) {
  const visible = usePageVisible();
  const intervals = useRefreshIntervals();
  const query = useQuery({
    queryKey: ["sector-groups", sector],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/sectors/groups", { params: { query: sector ? { sector } : {} }, signal })),
    enabled,
    refetchInterval: enabled && visible ? intervals.home_poll_seconds * 1000 : false,
    placeholderData: (previous) => previous?.sector === sector ? previous : undefined,
  });
  const data = enabled ? query.data : undefined;
  const quotes = useStockQuotes(data?.quote_symbols ?? [], [["sector-groups"]]);
  const pricesError = useSectorPrices(data?.prices_pending ?? [], true, { level: "group", sector });
  return { query, data, error: query.error ?? quotes.error ?? pricesError };
}

/** One shared range of candles for the sectors or the groups shown, aggregated by the backend. */
export function useSectorCandles(range: ChartRange, interval: Interval, enabled: boolean, level: Level = "sector", sector: string | null = null) {
  const scope = level === "group" ? { level, ...(sector ? { sector } : {}) } : {};
  return useQuery({
    queryKey: ["sector-candles", range, interval, level, sector],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/sectors/candles", { params: { query: { range, interval, ...scope } }, signal })),
    enabled,
    // Keep the old range on screen while a new one loads, but never another level's charts.
    placeholderData: (previous) => previous?.level === level && previous?.sector === (level === "group" ? sector : null) ? previous : undefined,
  });
}
