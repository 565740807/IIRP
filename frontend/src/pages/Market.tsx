import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowLeft, ChartCandlestick, LoaderCircle, RotateCcw } from "lucide-react";
import { tm } from "@/i18n";
import { client, unwrap } from "@/lib/api-client";
import { formatDay, formatEt, formatLocal, formatNumber, formatSignedPercent } from "@/lib/format";
import { sourceContext, sourceHref } from "@/lib/navigation";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { PriceChart } from "@/components/insider/PriceChart";

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 space-y-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm">{children}</dd>
    </div>
  );
}

/**
 * Index detail, opened from the home market strip: the saved quote with its
 * time and state, and the last six months of daily candles. The six months
 * are fetched once on opening (one request, kept 24 hours); the page then
 * only re-reads what is saved.
 */
export function MarketDetailPage() {
  const { t } = useTranslation();
  const { symbol = "" } = useParams();
  const location = useLocation();
  const queryClient = useQueryClient();
  const key = ["market", symbol];
  const query = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/market/{symbol}", { params: { path: { symbol } }, signal })),
    refetchInterval: (state) => (document.hidden ? false : state.state.data?.history.status === "fetching" ? 2_000 : 15_000),
  });
  const request = useMutation({
    mutationFn: (force: boolean) => unwrap(client.POST("/api/v1/market/{symbol}/history", { params: { path: { symbol }, query: { force } } })),
    onSuccess: (data) => queryClient.setQueryData(key, data),
  });
  const data = query.data;
  const asked = useRef<string | null>(null);
  useEffect(() => {
    if (data?.history.status === "missing" && asked.current !== symbol) {
      asked.current = symbol;
      request.mutate(false);
    }
  }, [data?.history.status, symbol]); // eslint-disable-line react-hooks/exhaustive-deps
  const quote = data?.quote;
  const percent = quote?.change_percent;
  const tone = percent == null ? "text-muted-foreground" : percent > 0 ? "text-up" : percent < 0 ? "text-down" : "";
  const state = quote?.freshness;
  const chart = data?.chart;
  const fetching = data?.history.status === "fetching" || request.isPending;
  return (
    <div className="space-y-4">
      <Link to={sourceHref(location.state, "/")} state={location.state?.fromState} className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" />
        {t("ui.market_detail.back")}
      </Link>
      {query.error && <p className="rounded-md border border-destructive/30 bg-card px-3 py-2 text-sm text-destructive">{query.error.message}</p>}
      {!data && !query.error && (
        <div className="space-y-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-80 w-full" />
        </div>
      )}
      {data && quote && (
        <>
          <section className="rounded-lg border bg-card px-5 py-4">
            <div className="flex flex-wrap items-start gap-x-6 gap-y-3">
              <div className="min-w-0">
                <h1 className="text-lg font-semibold tracking-tight">
                  {tm(quote.name)}
                  <span className="ml-2 text-sm font-normal text-muted-foreground">{symbol}</span>
                </h1>
                <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span className="text-3xl font-semibold tabular-nums">{quote.value == null ? "—" : formatNumber(quote.value, 2)}</span>
                  {quote.unit && <span className="text-sm text-muted-foreground">{tm(quote.unit)}</span>}
                  <span className={cn("text-lg font-medium tabular-nums", tone)}>
                    {formatSignedPercent(percent)}
                    {quote.change != null && <span className="ml-2 text-base">{quote.change > 0 ? "+" : quote.change < 0 ? "−" : ""}{formatNumber(Math.abs(quote.change), 2)}</span>}
                  </span>
                </div>
              </div>
              <div className="ml-auto flex items-center gap-2 text-sm">
                <span
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium",
                    state === "live" && "bg-up/10 text-up",
                    state === "closed" && "bg-secondary text-muted-foreground",
                    (state === "delayed" || state === "missing") && "bg-warn-soft text-warn",
                  )}
                >
                  <span className={cn("size-1.5 rounded-full", state === "live" ? "bg-up" : state === "closed" ? "bg-muted-foreground" : "bg-warn")} aria-hidden />
                  {t(`ui.market_detail.state.${state}`)}
                </span>
              </div>
            </div>
            {quote.reason && state !== "live" && <p className="mt-2 text-xs text-warn">{tm(quote.reason)}</p>}
            <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 border-t pt-3 sm:grid-cols-4">
              <Fact label={t(quote.status === "DAILY" ? "ui.market_detail.as_of_daily" : "ui.market_detail.as_of")}>
                {quote.status !== "DAILY" && quote.source_time ? (
                  <span title={t("ui.time.local", { time: formatLocal(quote.source_time) })}>{formatEt(quote.source_time)}</span>
                ) : (
                  formatDay(quote.as_of, { year: true })
                )}
              </Fact>
              <Fact label={t("ui.market_detail.fetched")}>
                {quote.fetched_at ? <span title={t("ui.time.local", { time: formatLocal(quote.fetched_at) })}>{formatEt(quote.fetched_at)}</span> : "—"}
              </Fact>
              <Fact label={t("ui.market_detail.previous_close")}>
                <span className="tabular-nums">{quote.previous_close == null ? "—" : formatNumber(quote.previous_close, 2)}</span>
                {quote.baseline_date && <span className="ml-1 text-xs text-muted-foreground">{formatDay(quote.baseline_date, { year: false })}</span>}
              </Fact>
              <Fact label={t("ui.market_detail.session")}>
                {quote.session ? `${formatEt(quote.session.start, { date: false, zone: false })} – ${formatEt(quote.session.end, { date: false })}` : "—"}
              </Fact>
            </dl>
            <p className="mt-3 text-xs text-muted-foreground">
              {[data.instrument && tm(data.instrument), data.delay && tm(data.delay), quote.baseline && tm(quote.baseline), data.source].filter(Boolean).join(" · ")}
            </p>
          </section>

          <section className="space-y-2">
            <header className="flex flex-wrap items-center gap-2">
              <h2 className="text-sm font-semibold">{t("ui.market_detail.chart_title")}</h2>
              {chart?.start && (
                <span className="text-xs text-muted-foreground">
                  {formatDay(chart.start, { year: true })} → {formatDay(chart.end, { year: true })}
                </span>
              )}
              <span className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
                {fetching ? (
                  <span className="inline-flex items-center gap-1.5">
                    <LoaderCircle className="size-3.5 motion-safe:animate-spin" />
                    {t("ui.market_detail.fetching")}
                  </span>
                ) : chart?.source && chart.source !== "quote" ? (
                  <span>{t("ui.analysis.fetched", { fetched: formatEt(chart.fetched_at), expires: formatEt(chart.expires_at) })}</span>
                ) : null}
                {data.history.status === "failed" && !fetching && (
                  <Button size="xs" variant="outline" onClick={() => request.mutate(true)}>
                    <RotateCcw />
                    {t("ui.analysis.progress.retry")}
                  </Button>
                )}
              </span>
            </header>
            {data.history.status === "failed" && data.history.reason && <p className="text-xs text-warn">{tm(data.history.reason)}</p>}
            {request.error && <p className="text-xs text-destructive">{request.error.message}</p>}
            <div className="rounded-lg border bg-card px-2 pt-2">
              {data.bars.length ? (
                <PriceChart bars={data.bars} height={360} />
              ) : (
                <div className="grid h-60 place-items-center text-sm text-muted-foreground">
                  {fetching ? <Skeleton className="h-52 w-full" /> : t("ui.market_detail.no_bars")}
                </div>
              )}
              <p className="px-2 pb-2 text-xs text-muted-foreground">
                {t(chart?.source === "quote" ? "ui.market_detail.legend_short" : "ui.market_detail.legend", { days: chart?.days ?? 186 })}
              </p>
            </div>
          </section>

          <div>
            <Button asChild variant="outline" size="sm">
              <Link state={sourceContext(location)} to={`/analysis/monthly?tickers=${encodeURIComponent(symbol)}`}>
                <ChartCandlestick />
                {t("ui.market_detail.monthly")}
              </Link>
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
