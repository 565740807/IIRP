import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { motion } from "motion/react";
import { ChevronRight, Download, LoaderCircle } from "lucide-react";
import { benchmarkName, pct } from "@/lib/analysis";
import { conclusion, context, exportHref, type EventAnalysis, type EventKind, type EventResult } from "@/lib/events";
import { formatDay, formatEt, formatLocal } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { PathChart, ReactionCandles } from "@/components/events/EventCharts";
import { EventDetailTable, QuarterTable, TickerRankTable, WindowTable } from "@/components/events/EventTables";

function Section({ title, actions, children }: { title: React.ReactNode; actions?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <header className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">{title}</h2>
        {actions && <div className="ml-auto flex items-center gap-1.5">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

/** Fetched/expiry times (D14) and the CSV exports that keep a result after 24 hours. */
export function EventDataLine({ analysis, refreshing }: { analysis: EventAnalysis; refreshing: boolean }) {
  const { t } = useTranslation();
  const fresh = analysis.freshness as { expired?: boolean; price_fetched_at?: string | null; price_expires_at?: string | null; research_cutoff?: string | null };
  const ready = analysis.tickers.some((item) => item.result);
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
      {refreshing || fresh.expired ? (
        <span className="inline-flex items-center gap-1.5">
          <LoaderCircle className="size-3.5 motion-safe:animate-spin" />
          {t("ui.analysis.expired_refetching")}
        </span>
      ) : fresh.price_fetched_at ? (
        <span title={t("ui.time.local", { time: formatLocal(fresh.price_fetched_at) })}>
          {t("ui.analysis.fetched", { fetched: formatEt(fresh.price_fetched_at), expires: formatEt(fresh.price_expires_at) })}
        </span>
      ) : null}
      <span>{t("ui.analysis.cutoff", { day: formatDay(analysis.cutoff_date, { year: true }) })}</span>
      {ready && (
        <span className="ml-auto inline-flex items-center gap-1">
          <span>{t("ui.analysis.export")}</span>
          {(["stats", "detail"] as const).map((table) => (
            <Button key={table} asChild size="sm" variant="outline" className="h-7 text-xs">
              <a href={exportHref(analysis.id, table)} download>
                <Download />
                {t(`ui.analysis.export_${table}`)}
              </a>
            </Button>
          ))}
        </span>
      )}
    </div>
  );
}

/** The answer first: one factual sentence (numbers from the server), the other windows, and where this ticker stands. */
function ConclusionCard({ kind, result, results, benchmark }: { kind: EventKind; result: EventResult; results: EventResult[]; benchmark: string | null }) {
  const { t } = useTranslation();
  const ranked = results.filter((item) => item.summary.reaction?.median != null).sort((a, b) => Number(b.summary.reaction.median) - Number(a.summary.reaction.median));
  const second = context(result, result.n, benchmark);
  return (
    <motion.section key={result.symbol} initial={{ opacity: 0.4 }} animate={{ opacity: 1 }} transition={{ duration: 0.2 }} className="rounded-lg border bg-card px-4 py-3">
      <p className="text-base leading-relaxed font-medium">{conclusion(result.symbol, kind, result)}</p>
      {second && <p className="mt-1 text-sm text-muted-foreground">{second}</p>}
      {ranked.length > 1 && (
        <p className="mt-1 text-sm text-muted-foreground">
          {t("ui.events.conclusion.across", {
            count: ranked.length,
            high: pct(ranked[0].summary.reaction.median),
            high_ticker: ranked[0].symbol,
            low: pct(ranked.at(-1)!.summary.reaction.median),
            low_ticker: ranked.at(-1)!.symbol,
          })}
        </p>
      )}
    </motion.section>
  );
}

/** How the numbers are calculated; last on the page and folded. */
export function EventMethod({ n }: { n: number }) {
  const { t } = useTranslation();
  const items = ["r", "base", "windows", "overlap", "candle", "path", "benchmark", "up", "quartiles", "missing", "prices", "cache"];
  return (
    <details className="group rounded-lg border bg-card">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-4 py-2.5 text-sm font-semibold">
        <ChevronRight className="size-4 text-muted-foreground transition-transform group-open:rotate-90" />
        {t("ui.analysis.method.title")}
      </summary>
      <ul className="list-disc space-y-1 px-4 pb-3 pl-9 text-sm text-muted-foreground">
        {items.map((item) => (
          <li key={item}>{t(`ui.events.method.${item}`, { n })}</li>
        ))}
      </ul>
    </details>
  );
}

/**
 * The results of one ticker (several tickers: chips above choose which):
 * conclusion → one candle per event → average path → statistics (and by
 * quarter, and the tickers ranked) → every event.
 */
export function EventResults({ analysis, kind, focused, onFocus }: { analysis: EventAnalysis; kind: EventKind; focused: string; onFocus: (symbol: string) => void }) {
  const { t } = useTranslation();
  const [showBenchmark, setShowBenchmark] = useState(true);
  const results = useMemo(() => analysis.tickers.flatMap((item) => (item.result ? [item.result] : [])), [analysis]);
  const result = results.find((item) => item.symbol === focused) ?? results[0];
  if (!result) return null;
  const benchmark = analysis.benchmark ?? null;
  const pairing = result.benchmark;
  const n = result.n;
  return (
    <>
      <ConclusionCard kind={kind} result={result} results={results} benchmark={benchmark} />
      {pairing && pairing.status !== "available" && (
        <p className="rounded-md border border-warn/30 bg-warn-soft px-3 py-2 text-xs text-warn">{t("ui.analysis.benchmark_pending", { name: benchmarkName(pairing.symbol) })}</p>
      )}
      <Section title={t("ui.events.chart_candles", { ticker: result.symbol, count: result.event_count })}>
        <div className="rounded-lg border bg-card px-2 pt-2">
          <ReactionCandles result={result} benchmark={benchmark} title={`${result.symbol} ${t("ui.events.window.reaction")}`} />
          <p className="px-2 pb-2 text-xs text-muted-foreground">{t(benchmark ? "ui.events.candles_legend_benchmark" : "ui.events.candles_legend")}</p>
        </div>
      </Section>
      <Section
        title={t("ui.events.chart_path", { ticker: result.symbol, n })}
        actions={
          benchmark ? (
            <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
              <input type="checkbox" checked={showBenchmark} onChange={(e) => setShowBenchmark(e.target.checked)} className="accent-primary" />
              {t("ui.events.show_benchmark", { benchmark: benchmarkName(benchmark) })}
            </label>
          ) : undefined
        }
      >
        <div className="rounded-lg border bg-card px-2 pt-2">
          <PathChart result={result} benchmark={benchmark} showBenchmark={showBenchmark} title={`${result.symbol} R−${n} … R+${n}`} />
          <p className="px-2 pb-2 text-xs text-muted-foreground">{t("ui.events.path_legend", { n })}</p>
        </div>
      </Section>
      <Section title={t("ui.events.stats_title", { ticker: result.symbol })}>
        <WindowTable result={result} benchmark={benchmark} />
      </Section>
      {kind === "earnings" && (result.quarters?.length ?? 0) > 0 && (
        <Section title={t("ui.events.quarters_title", { ticker: result.symbol })}>
          <QuarterTable result={result} benchmark={benchmark} />
        </Section>
      )}
      {results.length > 1 && (
        <Section title={t("ui.events.rank_title")}>
          <TickerRankTable results={results} benchmark={benchmark} selected={result.symbol} onSelect={onFocus} />
        </Section>
      )}
      <Section title={t("ui.events.detail_title", { ticker: result.symbol, count: result.event_count })}>
        <p className={cn("-mt-1 text-xs text-muted-foreground")}>{t("ui.events.detail_hint")}</p>
        <EventDetailTable result={result} benchmark={benchmark} />
      </Section>
    </>
  );
}
