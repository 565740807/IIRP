import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useWindowVirtualizer } from "@tanstack/react-virtual";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { ArrowUp, LoaderCircle } from "lucide-react";
import { client, unwrap, ApiError } from "@/lib/api-client";
import {
  appendPage,
  applyDelta,
  canApplyInPlace,
  freshMark,
  type FeedGroup,
  type FeedOrder,
  type FeedStream,
} from "@/lib/feedStream";
import { readFeedWithRetry } from "@/feedRecovery";
import { formatEt } from "@/lib/format";
import { usePageVisible, useRefreshIntervals } from "@/lib/refresh";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useInsiderN, useTradeWindows } from "@/lib/windows";
import { NControl } from "@/components/insider/NControl";
import { INDICES, useOverviewChoice, useVisibleStocks, useStockQuotes, type IndexFilter as Index } from "@/lib/insiderOverview";
import { IndexFilter, IndexStatus } from "@/components/insider/IndexFilter";
import { CurrentQuoteStatus, MismatchRatio } from "@/components/insider/InsiderPrice";
import { CARD_COLUMNS, FeedCard, FeedWindows, FeedPrices, LINE_COLUMNS } from "./FeedCard";

const FILTERS = ["focus", "buy", "sell", "derivative", "other", "all"] as const;
type Filter = (typeof FILTERS)[number];

function streamKey(filter: Filter, order: FeedOrder, index: Index) {
  return ["feed-stream", filter, order, index] as const;
}

/** Reads every delta page between the list's watermark and a new one. */
async function readDelta(latest: string, current: () => boolean) {
  const groups: FeedGroup[] = [];
  let removed: string[] = [];
  let target = "";
  let cursor = "";
  do {
    const page = await readFeedWithRetry(
      () =>
        unwrap(client.GET("/api/v1/feed/updates", {
          params: { query: { session_id: latest, include_groups: true, ...(target ? { target_session_id: target } : {}), ...(cursor ? { cursor } : {}) } },
        })),
      current,
    );
    if (!page) return null;
    target = page.target_session_id;
    cursor = page.next_cursor ?? "";
    groups.push(...page.groups);
    if (page.removed_ids.length) removed = page.removed_ids;
  } while (cursor);
  return { groups, removed, target };
}

/**
 * The home waterfall. At the top, newly published filings drop in at once;
 * while the reader is further down nothing moves and a "new trades" pill
 * waits. Scrolling down loads older groups of the same reading session.
 */
export function InsiderStream() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const filter = (FILTERS as readonly string[]).includes(params.get("filter") ?? "") ? (params.get("filter") as Filter) : "focus";
  const order: FeedOrder = params.get("order") === "transaction" ? "transaction" : "accepted";
  const [indexChoice, setIndex] = useOverviewChoice("index", INDICES, "all", "home");
  const index = indexChoice as Index;
  useEffect(() => {
    if (!params.has("index")) setIndex(index);
  }, [params, index]);
  const key = streamKey(filter, order, index);
  const visible = usePageVisible();
  const intervals = useRefreshIntervals();
  const reduceMotion = useReducedMotion();

  // The list lives in the query cache, so returning to the page shows it at once.
  const stream = useQuery({
    queryKey: key,
    queryFn: async (): Promise<FeedStream> => {
      const page = await unwrap(client.GET("/api/v1/feed", { params: { query: { type: filter, order, index } } }));
      return {
        session: page.session_id, latest: page.session_id, order, groups: page.groups,
        nextCursor: page.next_cursor, asOf: page.as_of, fresh: {}, dropped: [],
      };
    },
    staleTime: Infinity,
    gcTime: 30 * 60_000,
    refetchOnWindowFocus: false,
  });
  const data = stream.data;
  const update = useCallback(
    (change: (current: FeedStream) => FeedStream) => queryClient.setQueryData<FeedStream>(key, (current) => (current ? change(current) : current)),
    [queryClient, filter, order, index],
  );

  // How many groups changed since the list's watermark (local read only).
  const updates = useQuery({
    queryKey: ["feed-updates", data?.latest],
    queryFn: () => unwrap(client.GET("/api/v1/feed/updates", { params: { query: { session_id: data!.latest } } })),
    enabled: !!data?.latest,
    refetchInterval: visible ? intervals.feed_poll_seconds * 1000 : false,
    refetchOnWindowFocus: false,
  });
  // A reading session expires after 12 hours: start a new one.
  useEffect(() => {
    if (updates.error instanceof ApiError && [400, 409].includes(updates.error.status))
      void queryClient.resetQueries({ queryKey: key });
  }, [updates.error]);
  const pending = updates.data?.new_count ?? 0;

  const [atTop, setAtTop] = useState(true);
  useEffect(() => {
    const onScroll = () => setAtTop(canApplyInPlace(window.scrollY, false));
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  const applying = useRef(false);
  const [merging, setMerging] = useState(false);
  const scope = useRef(0);
  useEffect(() => {
    scope.current += 1;
  }, [filter, order, index]);
  const apply = useCallback(async () => {
    const current = queryClient.getQueryData<FeedStream>(key);
    if (!current || applying.current) return;
    applying.current = true;
    setMerging(true);
    const started = scope.current;
    try {
      const delta = await readDelta(current.latest, () => scope.current === started);
      if (!delta || scope.current !== started) return;
      update((latest) => applyDelta(latest, delta.groups, delta.removed, delta.target).stream);
      queryClient.setQueryData(["feed-updates", delta.target], { ...updates.data, new_count: 0 });
    } catch {
      // The pill stays; the next poll tries again.
    } finally {
      applying.current = false;
      setMerging(false);
    }
  }, [queryClient, update, filter, order, index]);

  // At the top (and nothing selected), new filings drop in without asking.
  useEffect(() => {
    if (!pending || !visible || !atTop || applying.current) return;
    if (window.getSelection()?.toString()) return;
    void apply();
  }, [pending, visible, atTop, apply, updates.dataUpdatedAt]);

  // The pill returns to the top first; the new cards then drop in there.
  const showNew = async () => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    await new Promise<void>((resolve) => {
      if (window.scrollY === 0) return resolve();
      const done = () => {
        window.removeEventListener("scrollend", done);
        window.clearTimeout(timer);
        resolve();
      };
      const timer = window.setTimeout(done, 700);
      window.addEventListener("scrollend", done);
      window.scrollTo({ top: 0, behavior: reduced ? "auto" : "smooth" });
    });
    window.scrollTo({ top: 0 });
    await apply();
  };

  // Older pages of the same session when the end of the list comes near.
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<Error | null>(null);
  const loadMore = useCallback(async () => {
    const current = queryClient.getQueryData<FeedStream>(key);
    if (!current?.nextCursor || loadingMore) return;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await unwrap(client.GET("/api/v1/feed", {
        params: { query: { session_id: current.session, cursor: current.nextCursor, type: filter, order, index } },
      }));
      update((latest) => appendPage(latest, page.groups, page.next_cursor));
    } catch (error) {
      setMoreError(error as Error);
    } finally {
      setLoadingMore(false);
    }
  }, [queryClient, update, loadingMore, filter, order, index]);

  const groups = data?.groups ?? [];
  const list = useRef<HTMLDivElement>(null);
  const { n } = useInsiderN();
  const [offset, setOffset] = useState(0);
  useLayoutEffect(() => {
    setOffset(list.current ? list.current.getBoundingClientRect().top + window.scrollY : 0);
  }, [stream.isPending]);
  const virtual = useWindowVirtualizer({
    count: groups.length,
    estimateSize: () => 34,
    overscan: 10,
    gap: 4,
    scrollMargin: offset,
    getItemKey: (index) => groups[index].id,
  });
  const items = virtual.getVirtualItems();
  const stocks = useVisibleStocks(list, items.map((item) => groups[item.index].revision_id).join(","));
  const { error: quoteError } = useStockQuotes(stocks.symbols);
  const revisions = stocks.ids.join(",");
  const prices = useQuery({
    queryKey: ["insider-feed-prices", revisions],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/insider/feed-prices", { params: { query: { revisions } }, signal })),
    enabled: visible && !!revisions,
    refetchInterval: visible ? intervals.home_poll_seconds * 1000 : false,
    // Rows still on screen keep their prices and checks while the next set loads.
    placeholderData: keepPreviousData,
  });
  const keys = useMemo(() => groups.filter((group) => stocks.ids.includes(group.revision_id)).flatMap((group) => group.trader_groups.map((trader) => ({ ticker: group.ticker, date: trader.transaction_date, issuer_id: group.issuer_id }))), [groups, revisions]);
  const windows = useTradeWindows(keys, n, visible);
  const shared = useMemo(() => ({ windows, n }), [windows, n]);
  const last = items.at(-1)?.index ?? -1;
  useEffect(() => {
    if (data?.nextCursor && !moreError && last >= groups.length - 5) void loadMore();
  }, [last, groups.length, data?.nextCursor, moreError, loadMore]);

  // Cards glide to their new places for a moment after a merge.
  const [gliding, setGliding] = useState(false);
  const previousLength = useRef(groups.length);
  useEffect(() => {
    if (groups.length === previousLength.current) return;
    previousLength.current = groups.length;
    setGliding(true);
    const timer = window.setTimeout(() => setGliding(false), 400);
    return () => window.clearTimeout(timer);
  }, [groups.length]);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 3000);
    return () => window.clearInterval(timer);
  }, []);

  const choose = (name: "filter" | "order", value: string) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set(name, value);
      return next;
    }, { replace: true });

  return (
    <section aria-labelledby="insider-heading" className="mt-4">
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-2">
        <h2 id="insider-heading" className="text-base font-semibold">{t("ui.feed.title")}</h2>
        {data && (
          <span className="text-xs text-muted-foreground tabular-nums">
            {merging ? (
              <span className="inline-flex items-center gap-1"><LoaderCircle className="size-3 motion-safe:animate-spin" />{t("ui.feed.merging")}</span>
            ) : t("ui.feed.as_of", { time: formatEt(data.asOf) })}
          </span>
        )}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <IndexFilter value={index} choose={setIndex}/>
          <NControl />
          <ToggleGroup type="single" size="sm" variant="outline" spacing={0} value={filter} aria-label={t("ui.feed.filter_label")}
            onValueChange={(value) => value && choose("filter", value)}>
            {FILTERS.map((item) => (
              <ToggleGroupItem key={item} value={item} className="px-2.5" title={item === "focus" ? t("ui.feed.filter_tip.focus") : undefined}>{t(`ui.feed.filter.${item}`)}</ToggleGroupItem>
            ))}
          </ToggleGroup>
          <ToggleGroup type="single" size="sm" variant="outline" spacing={0} value={order} aria-label={t("ui.feed.order_label")}
            onValueChange={(value) => value && choose("order", value)}>
            <ToggleGroupItem value="accepted" className="px-2.5">{t("ui.feed.order.accepted")}</ToggleGroupItem>
            <ToggleGroupItem value="transaction" className="px-2.5">{t("ui.feed.order.transaction")}</ToggleGroupItem>
          </ToggleGroup>
        </div>
      </div>

      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1"><IndexStatus value={index}/><CurrentQuoteStatus prices={Object.values(prices.data?.items ?? {})}/></div>
      <AnimatePresence>
        {pending > 0 && !atTop && (
          <motion.div
            key="pill"
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.2 }}
            className="pointer-events-none sticky top-16 z-20 flex h-0 justify-center"
          >
            <Button size="sm" onClick={showNew} className="pointer-events-auto rounded-full shadow-md" data-testid="new-trades">
              <ArrowUp />
              {t("ui.feed.new_trades", { count: pending })}
            </Button>
          </motion.div>
        )}
      </AnimatePresence>

      {quoteError && <p className="text-xs text-warn">{quoteError.message}</p>}
      {prices.error && <p className="text-xs text-warn">{prices.error.message}</p>}
      {stream.error && <p className="text-sm text-destructive">{stream.error.message}</p>}
      <div className={cn(CARD_COLUMNS, "border-l-[3px] border-transparent px-3 pb-1 text-[11px] font-medium text-muted-foreground")} aria-hidden>
        <span>{t("ui.feed.column.company")}</span>
        <span className={LINE_COLUMNS}>
          <span>{t("ui.feed.column.insider")}</span>
          <span>{t("ui.feed.column.action")}</span>
          <span className="text-right">{t("ui.feed.column.shares")}</span>
          <span className="text-right">{t("ui.feed.column.amount")}</span>
          <span className="text-right">{t("ui.overview.column.trade_price")}</span>
          <span className="text-right">{t("ui.overview.column.current")}</span>
          <span className="text-right">{t("ui.feed.column.traded")}</span>
          <span className="text-right" title={t("ui.window.before_head_long", { n })}>{t("ui.window.before_head", { n })}</span>
          <span className="text-right" title={t("ui.window.after_head_long", { n })}>{t("ui.window.after_head", { n })}</span>
        </span>
        <span className="text-right">{t("ui.feed.column.filed")}</span>
      </div>
      {stream.isPending ? (
        <div className="space-y-1" aria-busy>
          {Array.from({ length: 12 }, (_, index) => (
            <div key={index} className={cn(CARD_COLUMNS, "rounded-md border bg-card px-3 py-2")}>
              <Skeleton className="h-3.5 w-40" />
              <Skeleton className="h-3.5 w-full max-w-xl" />
              <Skeleton className="ml-auto h-3.5 w-16" />
            </div>
          ))}
        </div>
      ) : !groups.length ? (
        <p className="rounded-lg border bg-card px-4 py-10 text-center text-sm text-muted-foreground">{t("ui.feed.empty")}</p>
      ) : (
        <FeedWindows.Provider value={shared}>
        <FeedPrices.Provider value={prices.data?.items ?? {}}>
        <MismatchRatio.Provider value={prices.data?.price_mismatch_ratio ?? null}>
        <div ref={list} className="relative" style={{ height: virtual.getTotalSize() }} data-testid="feed-list">
          {items.map((item) => {
            const group = groups[item.index];
            const mark = data ? freshMark(data, group.id, now) : null;
            return (
              <div
                key={item.key}
                data-index={item.index}
                data-quote-symbol={group.quote_symbol ?? ""}
                data-price-ids={group.revision_id}
                ref={virtual.measureElement}
                className={cn("absolute inset-x-0 top-0", gliding && "transition-transform duration-300 ease-out")}
                style={{ transform: `translateY(${item.start - virtual.options.scrollMargin}px)` }}
              >
                <motion.div
                  initial={mark === "new" ? (reduceMotion ? { opacity: 0 } : { opacity: 0, y: -24 }) : false}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.28, ease: "easeOut", delay: mark === "new" ? Math.min(item.index, 8) * 0.05 : 0 }}
                >
                  <FeedCard group={group} mark={mark} />
                </motion.div>
              </div>
            );
          })}
        </div>
        </MismatchRatio.Provider>
        </FeedPrices.Provider>
        </FeedWindows.Provider>
      )}
      {groups.length > 0 && (
        <div className="flex h-12 items-center justify-center text-xs text-muted-foreground">
          {loadingMore ? (
            <span className="inline-flex items-center gap-1.5"><LoaderCircle className="size-3.5 motion-safe:animate-spin" />{t("ui.feed.loading_more")}</span>
          ) : moreError ? (
            <Button variant="ghost" size="sm" onClick={() => void loadMore()}>{t("ui.feed.retry_more")}</Button>
          ) : !data?.nextCursor ? (
            t("ui.feed.end")
          ) : null}
        </div>
      )}
    </section>
  );
}
