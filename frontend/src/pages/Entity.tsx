import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowLeft, Globe, LoaderCircle } from "lucide-react";
import { tm } from "@/i18n";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { formatCompact, formatDay, formatEt, formatMoney, formatNumber } from "@/lib/format";
import { actionLabel, readableCase, side } from "@/lib/insider";
import {
  DEFAULT_RANGE,
  parseRange,
  rangeParam,
  rangeQuery,
  saveRange,
  savedRange,
  type PageKind,
  type Range,
} from "@/lib/insiderPrefs";
import { useInsiderN, useTradeWindows } from "@/lib/windows";
import { sourceHref } from "@/researchStorage";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { NControl } from "@/components/insider/NControl";
import { PriceChart, type Marker } from "@/components/insider/PriceChart";
import { RangeControl } from "@/components/insider/RangeControl";
import { money, price, TradeTable, type Trade } from "@/components/insider/TradeTable";

type History = Schemas["EntityHistory"];
const PAGE = 200;
const MAX_ROWS = 2000;

/** Range from the link, else the one remembered for this page type, else 6 months. */
function useRange(kind: PageKind): [Range, (range: Range | null) => void] {
  const [params, setParams] = useSearchParams();
  const range = parseRange(params.get("range")) ?? savedRange(kind) ?? DEFAULT_RANGE;
  const change = (next: Range | null) => {
    saveRange(kind, next);
    setParams((previous) => {
      const updated = new URLSearchParams(previous);
      if (next === null || rangeParam(next) === rangeParam(DEFAULT_RANGE)) updated.delete("range");
      else updated.set("range", rangeParam(next));
      return updated;
    }, { replace: true });
  };
  return [range, change];
}

/** Every page of the range in one reading session (the server freezes the index). */
async function readHistory(kind: PageKind, id: string, range: Range, signal: AbortSignal) {
  const path = kind === "company" ? "/api/v1/companies/{entity_id}" : "/api/v1/people/{entity_id}";
  const base = rangeQuery(range);
  let cursor = "";
  let first: History | null = null;
  const items: Trade[] = [];
  do {
    const page = await unwrap(client.GET(path, { params: { path: { entity_id: id }, query: { ...base, limit: PAGE, ...(cursor ? { cursor } : {}) } }, signal }));
    first ??= page.data;
    items.push(...page.data.items);
    cursor = page.data.next_cursor ?? "";
  } while (cursor && items.length < MAX_ROWS);
  return { ...first!, items, truncated: !!cursor };
}

/** "Last 6 months: 4 insiders sold 277K shares, $92.12M in total; 1 bought …" */
function Headline({ data, kind, range }: { data: History; kind: PageKind; range: Range }) {
  const { t } = useTranslation();
  const scope =
    range.mode === "months"
      ? t("ui.entity.scope.months", { count: range.months })
      : range.mode === "count"
        ? t("ui.entity.scope.count", { count: data.total })
        : t("ui.entity.scope.dates", { start: formatDay(range.start, { year: true }), end: formatDay(range.end, { year: true }) });
  const parts = (["sell", "buy"] as const).flatMap((which) => {
    const total = data.headline.find((item) => item.side === which);
    if (!total) return [];
    const amount = total.known_amount == null ? null : formatMoney(total.known_amount, { currency: total.currencies === 1 && total.currency && total.currency !== "USD" ? total.currency : undefined });
    return [
      <span key={which} className={which === "buy" ? "text-up" : "text-down"}>
        {t(`ui.entity.headline.${which}_${kind}`, { count: total.owners, shares: formatCompact(total.shares), amount: amount ?? "—" })}
        {total.missing_price_rows > 0 && <span className="text-muted-foreground">{t("ui.entity.headline.partial", { count: total.missing_price_rows })}</span>}
      </span>,
    ];
  });
  return (
    <p className="text-sm">
      <span className="font-medium">{scope}</span>{" "}
      {parts.length ? (
        parts.reduce<React.ReactNode[]>((all, part, index) => (index ? [...all, <span key={`s${index}`}>{t("ui.entity.headline.separator")}</span>, part] : [part]), [])
      ) : (
        <span className="text-muted-foreground">{t("ui.entity.headline.none")}</span>
      )}
      <span className="text-muted-foreground">
        {" · "}
        {t("ui.entity.headline.rows", { count: data.total, filings: data.filings ?? 0 })}
      </span>
    </p>
  );
}

/** SEC fetch of this entity: start it, and show progress until the filings are parsed. */
function SecFetch({ kind, id, name, range }: { kind: PageKind; id: string; name: string; range: Range }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const state = useQuery({
    queryKey: ["entity-fetch", kind, id],
    queryFn: () => unwrap(client.GET("/api/v1/insider/fetch/{kind}/{cik}", { params: { path: { kind, cik: id } } })),
    refetchInterval: (query) => (["QUEUED", "RUNNING", "RETRY_WAIT"].includes(query.state.data?.fetch?.status ?? "") ? 2000 : false),
  });
  const fetch = state.data?.fetch;
  const running = !!fetch && ["QUEUED", "RUNNING", "RETRY_WAIT"].includes(fetch.status);
  const previous = useRef(running);
  useEffect(() => {
    // Parsed filings change the history: read it again.
    if (previous.current && !running) void queryClient.invalidateQueries({ queryKey: ["entity", kind, id] });
    if (running) void queryClient.invalidateQueries({ queryKey: ["entity", kind, id] });
    previous.current = running;
  }, [running, fetch?.parsed, kind, id, queryClient]);
  const start = useMutation({
    mutationFn: () =>
      unwrap(client.POST("/api/v1/insider/fetch", {
        body: {
          cik: id, kind, name,
          ...(range.mode === "months" ? { months: range.months } : range.mode === "dates" ? { start_date: range.start } : {}),
        },
      })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["entity-fetch", kind, id] }),
  });
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      {running ? (
        <span className="inline-flex items-center gap-1.5 rounded bg-secondary px-2 py-1 text-foreground">
          <LoaderCircle className="size-3.5 motion-safe:animate-spin" />
          {fetch.discovered == null
            ? t("ui.entity.fetch.listing")
            : t("ui.entity.fetch.progress", { parsed: fetch.parsed ?? 0, total: fetch.discovered })}
        </span>
      ) : fetch ? (
        <span title={tm(fetch.error) || undefined}>
          {t(fetch.status === "SUCCEEDED" ? "ui.entity.fetch.done" : "ui.entity.fetch.partial", {
            time: formatEt(fetch.created_at), start: formatDay(fetch.start_date, { year: true }),
          })}
        </span>
      ) : (
        <span>{t("ui.entity.fetch.local_only")}</span>
      )}
      {!running && (
        <Button size="xs" variant="outline" onClick={() => start.mutate()} disabled={start.isPending}>
          {start.isPending ? <LoaderCircle className="motion-safe:animate-spin" /> : <Globe />}
          {t("ui.entity.fetch.button", { range: range.mode === "months" ? t("ui.entity.scope.months_short", { count: range.months }) : range.mode === "dates" ? formatDay(range.start, { year: true }) : t("ui.entity.scope.months_short", { count: 6 }) })}
        </Button>
      )}
      {start.error && <span className="text-destructive">{start.error.message}</span>}
    </div>
  );
}

export function EntityPage({ kind }: { kind: PageKind }) {
  const { t } = useTranslation();
  const { id = "" } = useParams();
  const location = useLocation();
  const [range, setRange] = useRange(kind);
  const { n } = useInsiderN();
  const [selected, setSelected] = useState<string | null>(null);
  const history = useQuery({
    queryKey: ["entity", kind, id, rangeParam(range)],
    queryFn: ({ signal }) => readHistory(kind, id, range, signal),
    placeholderData: (previous) => (previous && previous.id === id ? previous : undefined),
  });
  const data = history.data;
  const rows = useMemo(() => data?.items ?? [], [data]);
  const name = readableCase(data && data.status !== "NOT_FETCHED" ? data.entity.name : (location.state as { name?: string } | null)?.name ?? data?.entity.name ?? id);

  const keys = useMemo(
    () => rows.map((row) => ({ ticker: row.ticker, date: row.date_anomaly ? null : row.transaction_date, issuer_id: row.issuer_id })),
    [rows],
  );
  const windows = useTradeWindows(keys, n, true, true);

  // The chart's company: the company itself, or on a person page the one chosen (most trades first).
  const companies = useMemo(() => {
    const counts = new Map<string, { ticker: string; name: string; issuer: string; count: number }>();
    for (const row of rows) {
      if (!row.ticker) continue;
      const item = counts.get(row.ticker) ?? { ticker: row.ticker, name: row.issuer_name ?? row.ticker, issuer: row.issuer_id, count: 0 };
      item.count += 1;
      counts.set(row.ticker, item);
    }
    return [...counts.values()].sort((a, b) => b.count - a.count);
  }, [rows]);
  const [chartTicker, setChartTicker] = useState<string | null>(null);
  const ticker = kind === "company" ? data?.entity.ticker ?? companies[0]?.ticker ?? null : chartTicker && companies.some((c) => c.ticker === chartTicker) ? chartTicker : companies[0]?.ticker ?? null;
  const chartRows = rows.filter((row) => row.ticker === ticker);
  const first = chartRows.map((row) => row.transaction_date).filter(Boolean).sort()[0];
  const chartStart = first ? new Date(Date.parse(`${first}T00:00:00Z`) - (n * 2 + 14) * 86_400_000).toISOString().slice(0, 10) : null;
  const tickerState = windows.ticker(ticker);
  const bars = useQuery({
    queryKey: ["insider-bars", ticker, chartStart, tickerState?.fetched_at],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/insider/bars", { params: { query: { ticker: ticker!, start_date: chartStart! } }, signal })),
    enabled: !!ticker && !!chartStart,
    placeholderData: (previous) => previous,
  });
  const markers = useMemo<Marker[]>(() => {
    const closes = new Map((bars.data?.bars ?? []).map((bar) => [bar.date, Number(bar.close)]));
    const trades = chartRows.filter((row) => side(row) && row.transaction_date && !row.date_anomaly);
    const largest = Math.max(1, ...trades.map((row) => Math.abs(Number(row.amount ?? 0))));
    return trades.flatMap((row) => {
      const level = row.price != null ? Number(row.price) : closes.get(row.transaction_date!);
      if (level == null || !Number.isFinite(level)) return [];
      const owner = row.owners.map((item) => readableCase(item.name)).join(", ");
      return [{
        id: row.id,
        date: row.transaction_date!,
        price: level,
        side: side(row)!,
        size: 9 + 13 * Math.sqrt(Math.abs(Number(row.amount ?? 0)) / largest),
        label: `${formatDay(row.transaction_date, { year: true })} · ${owner}<br/>${actionLabel(row, t)} · ${formatNumber(row.shares, 0)} @ ${price(row.price, row.currency)}${row.amount ? ` · ${money(row.amount, row.currency)}` : ""}`,
      }];
    });
  }, [chartRows, bars.data, t]);

  const select = (rowId: string) => {
    setSelected(rowId);
    document.getElementById(`trade-${rowId}`)?.scrollIntoView({ block: "center", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start gap-x-4 gap-y-1">
        <div className="min-w-0">
          <Link to={sourceHref(location.state, "/insiders")} state={(location.state as { fromState?: unknown } | null)?.fromState} className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
            <ArrowLeft className="size-3.5" />
            {t("ui.entity.back")}
          </Link>
          <h2 className="flex items-baseline gap-2 text-xl font-semibold tracking-tight">
            {kind === "company" && data?.entity.ticker && <span className="rounded bg-secondary px-1.5 text-base tabular-nums">{data.entity.ticker}</span>}
            <span className="truncate">{name}</span>
            <span className="text-xs font-normal text-muted-foreground tabular-nums">CIK {Number(id) || id}</span>
            {kind === "company" && tickerState && !tickerState.confirmed && tickerState.status === "ready" && (
              <Badge variant="outline" title={t("ui.entity.pending_tip")}>{t("ui.entity.pending")}</Badge>
            )}
          </h2>
        </div>
        <div className="ml-auto pt-4">
          <SecFetch kind={kind} id={id} name={name} range={range} />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <RangeControl range={range} onChange={setRange} />
        <NControl className="ml-auto" />
      </div>

      <section className="rounded-lg border bg-card px-3 pt-2 pb-1">
        <div className="flex flex-wrap items-center gap-2 pb-1 text-xs">
          {kind === "person" && companies.length > 1 ? (
            companies.slice(0, 8).map((company) => (
              <button key={company.ticker} type="button" onClick={() => setChartTicker(company.ticker)}
                className={cn("rounded border px-1.5 py-0.5 tabular-nums hover:bg-accent", company.ticker === ticker && "border-primary bg-primary text-primary-foreground hover:bg-primary")}>
                {company.ticker} <span className="opacity-70">{company.count}</span>
              </button>
            ))
          ) : (
            <span className="font-medium">{ticker ? t("ui.entity.chart_title", { ticker }) : t("ui.entity.chart_none")}</span>
          )}
          <span className="ml-auto inline-flex items-center gap-3 text-muted-foreground">
            <span className="inline-flex items-center gap-1"><span className="text-up">▲</span>{t("ui.entity.legend.buy")}</span>
            <span className="inline-flex items-center gap-1"><span className="text-down">▼</span>{t("ui.entity.legend.sell")}</span>
            {bars.data?.fetched_at && <span>{t("ui.entity.prices_as_of", { time: formatEt(bars.data.fetched_at) })}</span>}
          </span>
        </div>
        {bars.data?.bars.length ? (
          <PriceChart bars={bars.data.bars} markers={markers} selected={selected} onSelect={select} height={280} zoom />
        ) : (
          <div className="grid h-[280px] place-items-center text-sm text-muted-foreground">
            {!ticker ? (
              t("ui.entity.chart_none")
            ) : tickerState?.status === "fetching" || tickerState?.status === "not_fetched" || bars.isPending ? (
              <span className="inline-flex items-center gap-2"><LoaderCircle className="size-4 motion-safe:animate-spin" />{t("ui.entity.chart_fetching", { ticker })}</span>
            ) : (
              tm(tickerState?.reason) || t("ui.window.unavailable")
            )}
          </div>
        )}
      </section>

      {history.error ? (
        <p className="text-sm text-destructive">{history.error.message}</p>
      ) : !data ? (
        <div className="space-y-1.5">
          <Skeleton className="h-4 w-2/3" />
          {Array.from({ length: 8 }, (_, index) => <Skeleton key={index} className="h-8 w-full" />)}
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-baseline gap-2">
            <Headline data={data} kind={kind} range={range} />
            {history.isFetching && <LoaderCircle className="size-3.5 text-muted-foreground motion-safe:animate-spin" />}
          </div>
          {rows.length ? (
            <TradeTable rows={rows} kind={kind} personId={id} windows={windows} n={n} selected={selected} onSelect={setSelected} />
          ) : (
            <p className="rounded-lg border bg-card px-4 py-10 text-center text-sm text-muted-foreground">
              {data.status === "NOT_FETCHED" ? t("ui.entity.empty_not_fetched") : t("ui.entity.empty")}
            </p>
          )}
          {data.truncated && <p className="text-xs text-muted-foreground">{t("ui.entity.truncated", { count: rows.length })}</p>}
        </>
      )}
    </div>
  );
}
