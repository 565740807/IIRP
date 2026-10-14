import { lazy, Suspense, useContext, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Star } from "lucide-react";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { usePageVisible, useRefreshIntervals } from "@/lib/refresh";
import { refreshWatchViews, useWatchlist, WATCH_DEPENDENT, WATCHLIST_KEY } from "@/lib/watchlist";
import { newsByStock, WatchlistNewsSources, type WatchlistNewsItem } from "@/lib/watchlistNews";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

type InsiderNews = Schemas["InsiderNews"];
/** Codes named in the one-line notice; the rest are counted as "+n". */
const NAMED = 3;
const WatchlistNewsDetails = lazy(() => import("./WatchlistNewsDetails").then(module => ({ default: module.WatchlistNewsDetails })));
const InsiderNewsDetail = lazy(() => import("./InsiderNewsDetail").then(module => ({ default: module.InsiderNewsDetail })));

/** The public Insider entries, one per code, in the shape every source uses. */
function insiderItems(rows: readonly InsiderNews[] | undefined, t: (key: string, options?: Record<string, unknown>) => string): WatchlistNewsItem[] {
  return (rows ?? []).map((row) => ({
    key: `insider:${row.ticker}`,
    ticker: row.ticker,
    at: row.latest_at,
    summary: t("ui.watchlist.news.insider", { count: row.trades }),
    detail: <Suspense fallback={<span className="block h-6 animate-pulse rounded bg-muted" />}><InsiderNewsDetail row={row}/></Suspense>,
  }));
}

/** Every installed feature's entries; the sources are fixed for the application's life. */
function useFeatureNews() {
  const sources = useContext(WatchlistNewsSources);
  return sources.flatMap((useSource) => useSource() ?? []);
}

/**
 * One line under the index and sector strips when watched stocks have news since
 * the last "Got it": new open-market Insider trades (by SEC acceptance time) and
 * entries from installed features. "View" lists them; "Got it" acknowledges
 * everything read so far. With nothing new the line is not rendered at all.
 */
export function WatchlistNews() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const visible = usePageVisible();
  const intervals = useRefreshIntervals();
  const list = useWatchlist();
  const watching = (list.data?.items.length ?? 0) > 0;
  const news = useQuery({
    queryKey: ["watchlist-news", WATCH_DEPENDENT],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/watchlist/news", { signal })),
    enabled: watching,
    refetchInterval: visible ? intervals.overview_poll_seconds * 1000 : false,
  });
  const extra = useFeatureNews();
  const [acknowledged, setAcknowledged] = useState<string | null>(null);
  const acknowledge = useMutation({
    mutationFn: (at: string) => unwrap(client.PUT("/api/v1/watchlist/acknowledged", { body: { at } })),
    onMutate: (at) => setAcknowledged(at),
    onSuccess: (data) => {
      queryClient.setQueryData(WATCHLIST_KEY, data);
      refreshWatchViews(queryClient);
    },
    onError: () => setAcknowledged(null),
  });
  const names = useMemo(() => new Map((list.data?.items ?? []).map((item) => [item.ticker, item])), [list.data]);
  const insider = useMemo(() => new Map((news.data?.insider ?? []).map((row) => [row.ticker, row])), [news.data]);
  const stocks = newsByStock([...insiderItems(news.data?.insider, t), ...extra], acknowledged);
  // A line too long for the window shows in full on hover.
  const line = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const element = line.current;
    if (element) element.title = element.scrollWidth > element.clientWidth ? element.textContent ?? "" : "";
  });
  if (!watching || !news.data || !stocks.length) return null;
  const named = stocks.slice(0, NAMED);
  const rest = stocks.length - named.length;
  return (
    <section aria-label={t("ui.watchlist.news.label")} data-watchlist-news
      className="mt-3 flex min-w-0 items-center gap-2 rounded-lg border bg-card px-3 py-1.5 text-sm">
      <Star className="size-3.5 shrink-0 fill-current text-watch" aria-hidden/>
      <span className="shrink-0 font-medium">{t("ui.watchlist.news.title")}</span>
      <span ref={line} className="min-w-0 truncate">
        {named.map((stock, index) => (
          <span key={stock.ticker}>
            {index > 0 && <span className="text-muted-foreground"> · </span>}
            <span className="font-semibold">{stock.ticker}</span>{" "}
            {stock.summaries.map((part, partIndex) => (
              <span key={part.key}>{partIndex > 0 && t("ui.watchlist.news.joiner")}{part.node}</span>
            ))}
          </span>
        ))}
        {rest > 0 && <span className="text-muted-foreground"> · {t("ui.watchlist.news.more_stocks", { count: rest })}</span>}
      </span>
      <span className="ml-auto flex shrink-0 items-center gap-1">
        <Popover>
          <PopoverTrigger asChild>
            <Button size="xs" variant="ghost" data-watchlist-news-view>{t("ui.watchlist.news.view")}</Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="max-h-[min(32rem,70vh)] w-[min(32rem,calc(100vw-2rem))] space-y-3 overflow-y-auto p-3">
            <Suspense fallback={<span className="block h-32 animate-pulse rounded bg-muted" />}>
              <WatchlistNewsDetails at={news.data.acknowledged_at} stocks={stocks} insider={insider} names={names} />
            </Suspense>
          </PopoverContent>
        </Popover>
        <span className="text-muted-foreground" aria-hidden>|</span>
        <Button size="xs" variant="ghost" disabled={acknowledge.isPending}
          onClick={() => acknowledge.mutate(news.data.as_of)} data-watchlist-news-ack>
          {t("ui.watchlist.news.acknowledge")}
        </Button>
      </span>
      {acknowledge.error && <span role="alert" className={cn("shrink-0 text-xs text-destructive")}>{acknowledge.error.message}</span>}
    </section>
  );
}
