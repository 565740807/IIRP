import { useEffect, useRef } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import i18n, { locale } from "@/i18n";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { formatSignedRatio } from "@/lib/format";

/**
 * Monthly and interval research: requests, reading and labels. Every number
 * comes from the server (iirp.analysis.research); this file only formats.
 */
export type SeasonalKind = "monthly" | "interval";
export type Analysis = Schemas["AnalysisOutput"];
export type TickerResult = Schemas["AnalysisItem"];
export type Period = Schemas["Period"];
export type Candle = Schemas["Candle"];
export type PeriodStats = Schemas["PeriodStats"];
export type AnalysisInput = Schemas["AnalysisInput"];

export const MAX_TICKERS = 20;
export const DEFAULT_YEARS = 8;
export const BENCHMARKS = ["^GSPC", "^IXIC"] as const;
const ACTIVE = ["QUEUED", "RUNNING", "RETRY_WAIT", "PAUSE_REQUESTED", "CANCEL_REQUESTED", "WAITING"];

/** "AAPL, msft nvda" → ["AAPL", "MSFT", "NVDA"] (unique, in order). */
export function parseTickers(text: string) {
  return [...new Set(text.toUpperCase().split(/[\s,，;；]+/).map((item) => item.trim()).filter(Boolean))];
}

export function benchmarkName(symbol: string | null | undefined) {
  if (!symbol) return i18n.t("ui.analysis.benchmark.none");
  return (BENCHMARKS as readonly string[]).includes(symbol) ? i18n.t(`ui.analysis.benchmark.${symbol.slice(1)}`) : symbol;
}

/** "October" / "Oct"; Chinese always "10月". */
export function monthName(month: number, style: "long" | "short" = "long") {
  return new Intl.DateTimeFormat(locale(), { month: i18n.language === "zh" ? "short" : style, timeZone: "UTC" }).format(new Date(Date.UTC(2001, month - 1, 1)));
}

/** "09-20" → "Sep 20" / "9月20日". */
export function monthDay(mmdd: string) {
  const [month, day] = mmdd.split("-").map(Number);
  return new Intl.DateTimeFormat(locale(), { month: "short", day: "numeric", timeZone: "UTC" }).format(new Date(Date.UTC(2000, month - 1, day)));
}

export function periodName(period: Period) {
  return period.month ? monthName(period.month) : `${monthDay(period.start_mmdd)} → ${monthDay(period.end_mmdd)}`;
}

/** A year label: "2024", or "2024–25" for an interval that crosses the year end. */
export function yearLabel(candle: Candle, cross: boolean) {
  return cross ? `${candle.year}–${String(candle.year + 1).slice(2)}` : String(candle.year);
}

export function pct(value: unknown, digits = 1) {
  const text = formatSignedRatio(value, digits);
  const n = Number(value);
  // Even a small nonzero change rounded to 0.0% keeps its direction sign.
  return n > 0 && !text.startsWith("+") ? `+${text}` : n < 0 && !text.startsWith("−") ? `−${text}` : text;
}

/** Share 0.41 → "41%". */
export function share(value: unknown) {
  const n = Number(value);
  return value == null || !Number.isFinite(n) ? "—" : `${Math.round(n * 100)}%`;
}

/** The conclusion sentence for one ticker and one period. */
export function conclusion(symbol: string, period: Period, benchmark: string | null | undefined) {
  const t = i18n.t.bind(i18n);
  const stats = period.stats;
  const name = periodName(period);
  if (!stats.n) return t("ui.analysis.conclusion.empty", { period: name, ticker: symbol });
  const parts = [
    t("ui.analysis.conclusion.rose", { period: name, ticker: symbol, up: stats.up, n: stats.n, ratio: share(stats.up_ratio), median: pct(stats.median) }),
  ];
  if (benchmark && stats.paired_n)
    parts.push(t("ui.analysis.conclusion.beat", { benchmark: benchmarkName(benchmark), beat: stats.beat, n: stats.paired_n, excess: pct(stats.median_excess) }));
  return parts.join(t("ui.analysis.conclusion.join")) + t("ui.analysis.conclusion.end");
}

export function isActive(analysis: Analysis | undefined) {
  if (!analysis) return false;
  return ACTIVE.includes(analysis.status) || (analysis.progress ?? []).some((item) => !["done", "failed"].includes(item.step));
}

/**
 * One research by id: polled while it runs. Once its prices have expired
 * (24 hours) it is fetched again automatically and ``onReplaced`` moves
 * the page to the new research.
 */
export function useAnalysis(id: string | null, onReplaced: (id: string) => void) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["analysis", id],
    enabled: !!id,
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/analyses/{analysis_id}", { params: { path: { analysis_id: id! } }, signal })),
    refetchInterval: (state) => (isActive(state.state.data) ? (document.hidden ? 10_000 : 1_500) : false),
    placeholderData: keepPreviousData,
  });
  const replace = useRef(onReplaced);
  replace.current = onReplaced;
  const refresh = useMutation({
    mutationFn: (force: boolean) =>
      unwrap(client.POST("/api/v1/analyses/{analysis_id}/refresh", { params: { path: { analysis_id: id! }, query: { force } } })),
    onSuccess: (data) => {
      void queryClient.invalidateQueries({ queryKey: ["analysis"] });
      if (data.id !== id) replace.current(data.id);
    },
  });
  const expired = !!query.data?.freshness?.expired && query.data.id === id;
  const asked = useRef<string | null>(null);
  useEffect(() => {
    if (expired && id && asked.current !== id && !refresh.isPending) {
      asked.current = id;
      refresh.mutate(false);
    }
  }, [expired, id, refresh]);
  return { query, refresh };
}

export function useCreateAnalysis(onCreated: (id: string) => void) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: AnalysisInput) => unwrap(client.POST("/api/v1/analyses", { body })),
    onSuccess: (data) => {
      queryClient.setQueryData(["analysis", data.id], data);
      onCreated(data.id);
    },
  });
}

export function exportHref(id: string, table: "stats" | "detail") {
  return `/api/v1/analyses/${encodeURIComponent(id)}/export?table=${table}`;
}
