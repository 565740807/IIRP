import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { Link, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import NumberFlow from "@number-flow/react";
import { locale, tm } from "@/i18n";
import type { Schemas } from "@/lib/api-client";
import { formatDay, formatEt, formatLocal } from "@/lib/format";
import { homeQuery } from "@/lib/queries";
import { usePageVisible, useRefresh, useRefreshIntervals } from "@/lib/refresh";
import { sourceContext } from "@/lib/navigation";
import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

type Quote = Schemas["HomeQuote"];

/** Background flashes blue (up) or orange (down) for about a second when a value changes. */
export function useFlash(value: number | null | undefined) {
  const previous = useRef(value);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);
  useEffect(() => {
    const before = previous.current;
    previous.current = value;
    if (before == null || value == null || before === value) return;
    setFlash(value > before ? "up" : "down");
    const timer = window.setTimeout(() => setFlash(null), 1000);
    return () => window.clearTimeout(timer);
  }, [value]);
  return flash;
}

function Direction({ value, children, className }: { value: number | null | undefined; children: React.ReactNode; className?: string }) {
  return (
    <span className={cn("tabular-nums", value == null ? "text-muted-foreground" : value > 0 ? "text-up" : value < 0 ? "text-down" : "text-foreground", className)}>
      {children}
    </span>
  );
}

function QuoteTile({ quote, updating }: { quote: Quote; updating: boolean }) {
  const { t } = useTranslation();
  const location = useLocation();
  const flash = useFlash(quote.value);
  const percent = quote.change_percent == null ? null : quote.change_percent / 100;
  const delayed = quote.freshness === "delayed" && !updating;
  return (
    <Link
      to={`/market/${encodeURIComponent(quote.symbol)}`}
      state={sourceContext(location)}
      className={cn(
        "group flex min-w-0 flex-col gap-0.5 px-4 py-2.5 transition-colors duration-1000 hover:bg-accent/60",
        flash === "up" && "bg-up-flash duration-150",
        flash === "down" && "bg-down-flash duration-150",
      )}
    >
      <span className="flex items-center gap-1.5 truncate text-xs text-muted-foreground">
        {tm(quote.name)}
        {delayed && (
          <span className="size-1.5 shrink-0 rounded-full bg-warn motion-safe:animate-breathe" aria-label={t("ui.market.delayed")} />
        )}
      </span>
      {quote.value == null ? (
        <span className="text-base text-muted-foreground">—</span>
      ) : (
        <span className="text-[17px] leading-6 font-semibold tabular-nums">
          <NumberFlow value={quote.value} locales={locale()} format={{ minimumFractionDigits: 2, maximumFractionDigits: 2 }} />
        </span>
      )}
      <span className="flex items-baseline gap-2 text-xs">
        {percent == null ? (
          <span className="text-muted-foreground">{quote.freshness === "missing" ? tm(quote.reason) : t("ui.market.no_change")}</span>
        ) : (
          <>
            <Direction value={percent}>
              <NumberFlow
                value={percent}
                locales={locale()}
                format={{ style: "percent", minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "exceptZero" }}
              />
            </Direction>
            {quote.change != null && (
              <Direction value={quote.change} className="text-muted-foreground/90">
                <NumberFlow
                  value={quote.change}
                  locales={locale()}
                  format={{ minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "exceptZero" }}
                />
              </Direction>
            )}
          </>
        )}
      </span>
    </Link>
  );
}

/**
 * Delayed quotes grouped by reason: one sentence when they share it
 * ("5 quotes delayed: …"), otherwise one group per reason with its names.
 */
function delayGroups(delayed: Quote[]) {
  const groups = new Map<string, string[]>();
  for (const quote of delayed) {
    const reason = tm(quote.reason);
    groups.set(reason, [...(groups.get(reason) ?? []), tm(quote.name)]);
  }
  return [...groups.entries()].map(([reason, names]) => ({ reason, names }));
}

/** One line under the strip: when the data is from and whether it is live, closed, updating or delayed. */
function StripStatus({ quotes, updating }: { quotes: Quote[]; updating: boolean }) {
  const { t } = useTranslation();
  const delayed = quotes.filter((quote) => quote.freshness === "delayed");
  // Open or closed refers to the US stock market (S&P 500 session), not VIX or gold hours.
  const reference = quotes.find((quote) => quote.symbol === "^GSPC") ?? quotes[0];
  const updated = quotes.map((quote) => quote.fetched_at).filter(Boolean).sort().at(-1);
  if (!reference) return null;
  const closed = reference.freshness === "closed" || (reference.freshness !== "live" && reference.market_open === false);
  const groups = delayGroups(delayed);
  return (
    <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 px-1 pt-1.5 text-xs text-muted-foreground" role="status">
      {updating && delayed.length ? (
        <span className="inline-flex items-center gap-1.5">
          <LoaderCircle className="size-3 motion-safe:animate-spin" aria-hidden />
          {t("ui.market.updating")}
        </span>
      ) : delayed.length ? (
        <span className="inline-flex items-center gap-1.5 rounded bg-warn-soft px-1.5 py-0.5 font-medium text-warn">
          <span className="size-1.5 rounded-full bg-warn motion-safe:animate-breathe" aria-hidden />
          {groups.length === 1
            ? t("ui.market.delayed_all", { count: delayed.length, reason: groups[0].reason })
            : t("ui.market.delayed_some", { count: delayed.length })}
        </span>
      ) : closed ? (
        <span>{t("ui.market.closed", { day: formatDay(reference.as_of, { year: false }) })}</span>
      ) : (
        <span className="inline-flex items-center gap-1.5">
          <span className="size-1.5 rounded-full bg-up" aria-hidden />
          {t("ui.market.live")}
        </span>
      )}
      {!updating && groups.length > 1 && (
        <span className="text-warn">
          {groups.map((group) => t("ui.market.delay_group", { names: group.names.join(t("format.list_separator")), reason: group.reason })).join(" · ")}
        </span>
      )}
      {!updating && delayed.length > 0 && (
        <span className="tabular-nums">
          {t("ui.market.last_updated", { time: formatEt(delayed.map((quote) => quote.fetched_at).filter(Boolean).sort()[0]) })}
        </span>
      )}
      {updated && (
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="ml-auto cursor-default tabular-nums">{t("ui.market.updated", { time: formatEt(updated) })}</span>
          </TooltipTrigger>
          <TooltipContent>{t("ui.time.local", { time: formatLocal(updated) })}</TooltipContent>
        </Tooltip>
      )}
    </p>
  );
}

/**
 * Until the first update after opening has finished, saved quotes may look
 * delayed only because nobody has asked for new ones yet; show "Updating…".
 */
function useFirstUpdate(fetching: boolean) {
  const { refreshing } = useRefresh();
  const seen = useRef(false);
  const [done, setDone] = useState(false);
  useEffect(() => {
    if (refreshing) seen.current = true;
    else if (seen.current && !fetching) setDone(true);
  }, [refreshing, fetching]);
  // A check that never starts (offline service) must not keep the spinner forever.
  useEffect(() => {
    const timer = window.setTimeout(() => setDone(true), 20_000);
    return () => window.clearTimeout(timer);
  }, []);
  return !done;
}

/** Five indexes in one compact row; values are the server's saved quotes. */
export function MarketStrip() {
  const { t } = useTranslation();
  const visible = usePageVisible();
  const intervals = useRefreshIntervals();
  const query = useQuery({
    ...homeQuery,
    refetchInterval: visible ? intervals.home_poll_seconds * 1000 : false,
  });
  const quotes = query.data?.market ?? [];
  const updating = useFirstUpdate(query.isFetching);
  return (
    <section aria-label={t("ui.market.label")}>
      <div className="grid grid-cols-5 divide-x overflow-hidden rounded-lg border bg-card">
        {query.isPending
          ? Array.from({ length: 5 }, (_, index) => (
              <div key={index} className="flex flex-col gap-1.5 px-4 py-2.5">
                <Skeleton className="h-3 w-20" />
                <Skeleton className="h-5 w-24" />
                <Skeleton className="h-3 w-16" />
              </div>
            ))
          : quotes.map((quote) => <QuoteTile key={quote.symbol} quote={quote} updating={updating} />)}
      </div>
      {query.error ? (
        <p className="px-1 pt-1.5 text-xs text-destructive">{query.error.message}</p>
      ) : (
        <StripStatus quotes={quotes} updating={updating} />
      )}
    </section>
  );
}
