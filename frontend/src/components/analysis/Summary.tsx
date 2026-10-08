import { useTranslation } from "react-i18next";
import { motion } from "motion/react";
import { ChevronRight } from "lucide-react";
import { conclusion, pct, share, periodName, type Period, type SeasonalKind } from "@/lib/analysis";
import type { Schemas } from "@/lib/api-client";
import { directionClass, formatDay } from "@/lib/format";

/**
 * The answer first: one factual sentence for the chosen ticker and period
 * (numbers from the server), this year's change on its own line, and for
 * several tickers where the chosen one stands among them.
 */
export function ConclusionCard({
  symbol,
  period,
  benchmark,
  across,
}: {
  symbol: string;
  period: Period;
  benchmark: string | null;
  across?: Schemas["TickerComparison"];
}) {
  const { t } = useTranslation();
  const current = period.years.find((candle) => candle.current);
  return (
    <motion.section
      key={`${symbol}:${period.key}`}
      initial={{ opacity: 0.4 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.2 }}
      className="rounded-lg border bg-card px-4 py-3"
    >
      <p className="text-base leading-relaxed font-medium">{conclusion(symbol, period, benchmark)}</p>
      {period.stats.n > 0 && <p className="mt-1 text-xs text-muted-foreground">{t("ui.analysis.col.up_interval", { low: share(period.stats.up_low), high: share(period.stats.up_high) })}</p>}
      {current?.change != null && (
        <p className="mt-1 text-sm text-muted-foreground">
          {t(current.status === "in_progress" ? "ui.analysis.current_progress" : "ui.analysis.current_complete", {
            year: current.year,
            start: formatDay(current.start_date),
            end: formatDay(current.end_date),
          })}{" "}
          <span className={`font-medium ${directionClass(current.change)}`}>{pct(current.change, 2)}</span>
          {benchmark && current.excess != null && (
            <span>
              {" · "}
              {t("ui.analysis.current_excess")} <span className={directionClass(current.excess)}>{pct(current.excess, 2)}</span>
            </span>
          )}
        </p>
      )}
      {across && across.count > 1 && (
        <p className="mt-1 text-sm text-muted-foreground">
          {t("ui.analysis.across", {
            count: across.count,
            period: periodName(period),
            high: pct(across.high),
            high_ticker: across.high_ticker,
            low: pct(across.low),
            low_ticker: across.low_ticker,
            positive: across.positive,
          })}
        </p>
      )}
    </motion.section>
  );
}

/** How the numbers are calculated; last on the page and folded. */
export function Method({ kind }: { kind: SeasonalKind }) {
  const { t } = useTranslation();
  const items = [kind === "monthly" ? "candle_monthly" : "candle_interval", "change", "prices", "gap", "years", "up", "interval", "benchmark", "quartiles", "missing", "cache"];
  return (
    <details className="group rounded-lg border bg-card">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-4 py-2.5 text-sm font-semibold">
        <ChevronRight className="size-4 text-muted-foreground transition-transform group-open:rotate-90" />
        {t("ui.analysis.method.title")}
      </summary>
      <ul className="list-disc space-y-1 px-4 pb-3 pl-9 text-sm text-muted-foreground">
        {items.map((item) => (
          <li key={item}>{t(`ui.analysis.method.${item}`)}</li>
        ))}
      </ul>
    </details>
  );
}
