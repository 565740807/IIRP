import { useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import NumberFlow from "@number-flow/react";
import { locale, tm } from "@/i18n";
import type { Schemas } from "@/lib/api-client";
import { formatDay, formatEt, formatLocal } from "@/lib/format";
import { homeQuery } from "@/lib/queries";
import { usePageVisible, useRefreshIntervals } from "@/lib/refresh";
import { sourceContext } from "@/researchStorage";
import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

type Quote = Schemas["HomeQuote"];

/** Background flashes blue (up) or orange (down) for about a second when a value changes. */
function useFlash(value: number | null | undefined) {
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

function QuoteTile({ quote }: { quote: Quote }) {
  const { t } = useTranslation();
  const location = useLocation();
  const flash = useFlash(quote.value);
  const percent = quote.change_percent == null ? null : quote.change_percent / 100;
  const delayed = quote.freshness === "delayed";
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

/** One line under the strip: when the data is from and whether it is live, closed or delayed. */
function StripStatus({ quotes }: { quotes: Quote[] }) {
  const { t } = useTranslation();
  const delayed = quotes.filter((quote) => quote.freshness === "delayed");
  // Open or closed refers to the US stock market (S&P 500 session), not VIX or gold hours.
  const reference = quotes.find((quote) => quote.symbol === "^GSPC") ?? quotes[0];
  const updated = quotes.map((quote) => quote.fetched_at).filter(Boolean).sort().at(-1);
  if (!reference) return null;
  const closed = reference.freshness === "closed" || (reference.freshness !== "live" && reference.market_open === false);
  return (
    <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 px-1 pt-1.5 text-xs text-muted-foreground">
      {delayed.length ? (
        <span className="inline-flex items-center gap-1.5 rounded bg-warn-soft px-1.5 py-0.5 font-medium text-warn">
          <span className="size-1.5 rounded-full bg-warn motion-safe:animate-breathe" aria-hidden />
          {t("ui.market.delayed_since", { time: formatEt(delayed.map((quote) => quote.fetched_at).filter(Boolean).sort()[0], { date: true }) })}
        </span>
      ) : closed ? (
        <span>{t("ui.market.closed", { day: formatDay(reference.as_of, { year: false }) })}</span>
      ) : (
        <span className="inline-flex items-center gap-1.5">
          <span className="size-1.5 rounded-full bg-up" aria-hidden />
          {t("ui.market.live")}
        </span>
      )}
      {delayed.length > 0 && (
        <span className="text-warn">
          {[...new Set(delayed.map((quote) => `${tm(quote.name)}: ${tm(quote.reason)}`))].join(" · ")}
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
          : quotes.map((quote) => <QuoteTile key={quote.symbol} quote={quote} />)}
      </div>
      {query.error ? (
        <p className="px-1 pt-1.5 text-xs text-destructive">{query.error.message}</p>
      ) : (
        <StripStatus quotes={quotes} />
      )}
    </section>
  );
}
