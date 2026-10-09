import { useEffect, useRef } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import i18n from "@/i18n";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { benchmarkName, pct, share } from "@/lib/analysis";

/**
 * Earnings and custom events: saved sets, analyses, and the labels and
 * the conclusion sentence of their results. Every number comes from the
 * server (iirp.analysis.event_windows); this file only formats.
 */
export type EventKind = "earnings" | "custom";
export type EventAnalysis = Schemas["EventAnalysisOutput"];
export type EventResult = Schemas["EventTickerResult"];
export type EventRow = Schemas["EventRow"];
export type EventStats = Schemas["EventStatistics"];
export type EventItem = Schemas["EventItem"];
export type WindowKey = "before" | "reaction" | "gap" | "after";

export const WINDOWS: WindowKey[] = ["before", "gap", "reaction", "after"];
export const SESSIONS: EventItem["session"][] = ["before_open", "during", "after_close", "unknown"];
const ACTIVE = ["QUEUED", "RUNNING", "RETRY_WAIT", "PAUSE_REQUESTED", "CANCEL_REQUESTED", "WAITING"];

/** The analysis tab of each kind. */
export const TAB = { earnings: "earnings", custom: "events" } as const;

export const defaultsQuery = {
  queryKey: ["event-defaults"],
  queryFn: () => unwrap(client.GET("/api/v1/events/defaults")),
  staleTime: Infinity,
} as const;

/** "Before 5 sessions", "Reaction day", "Opening gap", "After 5 sessions". */
export function windowLabel(key: WindowKey, n: number) {
  return i18n.t(`ui.events.window.${key}`, { n });
}

export function windowFormula(key: WindowKey) {
  return i18n.t(`ui.events.formula.${key}`);
}

export function sessionLabel(session: string) {
  return i18n.t(`ui.events.session.${session}`);
}

/** "R−5", "R", "R+3". */
export function offsetLabel(offset: number) {
  const label = i18n.t("ui.events.window.reaction");
  return offset === 0 ? label : `${label}${offset > 0 ? "+" : "−"}${Math.abs(offset)}`;
}

/** FY2025 Q4 for earnings, else empty. */
export function fiscalLabel(row: { fiscal_year?: number | null; fiscal_quarter?: number | null }) {
  return row.fiscal_year ? `FY${row.fiscal_year} Q${row.fiscal_quarter}` : "";
}

/**
 * The answer first, in one factual sentence: the reaction day's median and up
 * share with its 95% interval, the median absolute move and the median gap.
 */
export function conclusion(symbol: string, kind: EventKind, result: EventResult) {
  const t = i18n.t.bind(i18n);
  const reaction = result.summary.reaction;
  const head = t(`ui.events.conclusion.head_${kind}`, { ticker: symbol, count: result.event_count });
  if (!reaction?.n) return t("ui.events.conclusion.empty", { head });
  return t("ui.events.conclusion.sentence", {
    head,
    median: pct(reaction.median),
    ratio: share(reaction.up_ratio),
    up: reaction.up,
    n: reaction.n,
    low: share(reaction.up_low),
    high: share(reaction.up_high),
    absolute: pct(reaction.abs_median).replace(/^\+/, ""),
    gap: pct(result.summary.gap?.median),
  });
}

/** The second line: before and after windows, and the benchmark on the reaction day. */
export function context(result: EventResult, n: number, benchmark: string | null | undefined) {
  const t = i18n.t.bind(i18n);
  const { before, after, reaction } = result.summary;
  const parts = [];
  if (before?.n) parts.push(t("ui.events.conclusion.window", { window: windowLabel("before", n), median: pct(before.median), up: before.up, n: before.n, ratio: share(before.up_ratio) }));
  if (after?.n) parts.push(t("ui.events.conclusion.window", { window: windowLabel("after", n), median: pct(after.median), up: after.up, n: after.n, ratio: share(after.up_ratio) }));
  if (benchmark && reaction?.paired_n)
    parts.push(t("ui.events.conclusion.beat", { benchmark: benchmarkName(benchmark), beat: reaction.beat, n: reaction.paired_n, excess: pct(reaction.median_excess) }));
  return parts.length ? parts.join(t("ui.analysis.conclusion.join")) + t("ui.analysis.conclusion.end") : "";
}

export function isActive(analysis: EventAnalysis | undefined) {
  if (!analysis) return false;
  return ACTIVE.includes(analysis.status) || analysis.progress.some((item) => !["done", "failed"].includes(item.step));
}

/**
 * One analysis by id: polled while it runs. Once its prices have expired
 * (24 hours) it is fetched again automatically and ``onReplaced`` moves
 * the page to the new analysis.
 */
export type EventFilters = { quarter?: number; recent_years?: number; session?: EventItem["session"]; direction?: "up" | "down" };

export function useEventAnalysis(id: string | null, onReplaced: (id: string) => void, filters: EventFilters = {}) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["event-analysis", id, filters],
    enabled: !!id,
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/events/analyses/{analysis_id}", { params: { path: { analysis_id: id! }, query: filters }, signal })),
    refetchInterval: (state) => (isActive(state.state.data) ? (document.hidden ? 10_000 : 1_500) : false),
    placeholderData: keepPreviousData,
  });
  const replace = useRef(onReplaced);
  replace.current = onReplaced;
  const refresh = useMutation({
    mutationFn: (force: boolean) =>
      unwrap(client.POST("/api/v1/events/analyses/{analysis_id}/refresh", { params: { path: { analysis_id: id! }, query: { force } } })),
    onSuccess: (data) => {
      queryClient.setQueryData(["event-analysis", data.id], data);
      if (data.id !== id) replace.current(data.id);
    },
  });
  const expired = !!(query.data?.freshness as { expired?: boolean } | undefined)?.expired && query.data?.id === id;
  const asked = useRef<string | null>(null);
  useEffect(() => {
    if (expired && id && asked.current !== id && !refresh.isPending) {
      asked.current = id;
      refresh.mutate(false);
    }
  }, [expired, id, refresh]);
  return { query, refresh };
}

/** Another n or benchmark for the analysis shown; the server reuses one made today. */
export function useVariant(onCreated: (id: string) => void) {
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; n?: number; benchmark?: string | null }) =>
      unwrap(client.POST("/api/v1/events/analyses/{analysis_id}/variant", {
        params: { path: { analysis_id: id } },
        body: { request_id: crypto.randomUUID(), ...body },
      })),
    onSuccess: (data) => onCreated(data.analysis_id),
  });
}

export function useEventSets(kind: EventKind) {
  return useQuery({
    queryKey: ["event-sets", kind],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/events/sets", { params: { query: { kind } }, signal })),
  });
}

export function exportHref(id: string, table: "stats" | "detail", filters: EventFilters = {}) {
  const params = new URLSearchParams({ table });
  Object.entries(filters).forEach(([key, value]) => { if (value != null) params.set(key, String(value)); });
  return `/api/v1/events/analyses/${encodeURIComponent(id)}/export?${params}`;
}

/** The pasted JSON for a list of events (reaction dates dropped, empty fields left out). */
export function eventsJson(events: EventItem[]) {
  return JSON.stringify(
    {
      events: events.map(({ reaction_date: _reaction, ...rest }) =>
        Object.fromEntries(Object.entries(rest).filter(([, value]) => value != null && value !== "")),
      ),
    },
    null,
    1,
  );
}

function read(key: string) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* Optional: the link still carries the choice. */
  }
}

/**
 * n per feature: from the link (?n=), else the remembered choice, else the
 * configured default; the benchmark is remembered the same way (default S&P 500).
 */
export function useEventChoices(kind: EventKind) {
  const [params, setParams] = useSearchParams();
  const defaults = useQuery(defaultsQuery).data;
  const defaultN = defaults?.window_sessions[kind] ?? 5;
  const min = defaults?.min ?? 1;
  const max = defaults?.max ?? 60;
  const parse = (value: string | null) => {
    const n = Number(value);
    return value && Number.isInteger(n) && n >= min && n <= max ? n : null;
  };
  const n = parse(params.get("n")) ?? parse(read(`iirp.events.n.${kind}`)) ?? defaultN;
  const stored = read(`iirp.events.benchmark.${kind}`);
  const benchmark = stored === null ? (defaults?.benchmark ?? "^GSPC") : stored === "" ? null : stored;
  const setN = (value: number | null) => {
    write(`iirp.events.n.${kind}`, value === null || value === defaultN ? null : String(value));
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      if (value === null || value === defaultN) next.delete("n");
      else next.set("n", String(value));
      return next;
    }, { replace: true });
  };
  const setBenchmark = (value: string | null) => write(`iirp.events.benchmark.${kind}`, value ?? "");
  return { n, setN, defaultN, min, max, presets: defaults?.presets ?? [3, 5, 10, 20], benchmark, setBenchmark };
}
