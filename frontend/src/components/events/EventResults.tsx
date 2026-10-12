import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { motion } from "motion/react";
import { ChevronRight, Download } from "lucide-react";
import { benchmarkName, pct, share, type Basis } from "@/lib/analysis";
import { conclusion, context, exportHref, type EventAnalysis, type EventKind, type EventResult, type EventFilters } from "@/lib/events";
import { BasisChoice, SectorNote } from "@/components/analysis/References";
import { Button } from "@/components/ui/button";
import { EventKline, PathChart, ReactionCandles } from "@/components/events/EventCharts";
import { SourceLine } from "@/components/analysis/CacheTimes";
import { FoldSection } from "@/components/ui/fold";
import { InfoTip } from "@/components/ui/info";
import { PageContents } from "@/components/ui/page-contents";
import { EventDistributions, WindowCards } from "@/components/events/EventDistributions";
import { EventDetailTable, QuarterTable, TickerRankTable, WindowTable } from "@/components/events/EventTables";
import { ExpectationsTable, GroupStats, RowContext, Timeline } from "@/components/events/EventContext";

function Section({ id, title, info, actions, children }: { id?: string; title: React.ReactNode; info?: React.ReactNode; actions?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section id={id} className="scroll-mt-28 space-y-2">
      <header className="flex flex-wrap items-center gap-2">
        <h2 className="section-title">{title}</h2>
        {info && <InfoTip>{info}</InfoTip>}
        {actions && <div className="ml-auto flex items-center gap-1.5">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

/** Where the prices came from (one line, details on request) and the CSV exports that keep a result after 24 hours. */
export function EventDataLine({ analysis, refreshing, filters }: { analysis: EventAnalysis; refreshing: boolean; filters: EventFilters }) {
  const { t } = useTranslation();
  const fresh = analysis.freshness;
  const ready = analysis.tickers.some((item) => item.result);
  return (
    <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
      <SourceLine sources={fresh.sources ?? []} expires={fresh.price_expires_at} cutoff={analysis.cutoff_date} refreshing={refreshing || fresh.expired} />
      {ready && (
        <span className="ml-auto inline-flex items-center gap-1 text-xs text-muted-foreground">
          <span>{t("ui.analysis.export")}</span>
          {(["stats", "detail"] as const).map((table) => (
            <Button key={table} asChild size="sm" variant="outline" className="h-7 text-xs">
              <a href={exportHref(analysis.id, table, filters)} download>
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
function ConclusionCard({ kind, result, across, benchmark }: { kind: EventKind; result: EventResult; across: EventAnalysis["comparison"]; benchmark: string | null }) {
  const { t } = useTranslation();

  const second = context(result, result.n, benchmark);
  return (
    <motion.section key={result.symbol} initial={{ opacity: 0.4 }} animate={{ opacity: 1 }} transition={{ duration: 0.2 }} className="rounded-lg border bg-card px-4 py-3">
      <p className="text-base leading-relaxed font-medium">{conclusion(result.symbol, kind, result)}</p>
      {result.summary.reaction.n > 0 && <p className="mt-1 text-xs text-muted-foreground">{t("ui.analysis.col.up_interval", { low: share(result.summary.reaction.up_low), high: share(result.summary.reaction.up_high) })}</p>}
      {second && <p className="mt-1 text-sm text-muted-foreground">{second}</p>}
      {across.count > 1 && (
        <p className="mt-1 text-sm text-muted-foreground">
          {t("ui.events.conclusion.across", {
            count: across.count,
            high: pct(across.high),
            high_ticker: across.high_ticker,
            low: pct(across.low),
            low_ticker: across.low_ticker,
          })}
        </p>
      )}
    </motion.section>
  );
}

/** How the numbers are calculated; last on the page and folded. */
export function EventMethod({ n }: { n: number }) {
  const { t } = useTranslation();
  const items = ["r", "base", "windows", "overlap", "candle", "path", "benchmark", "up", "quartiles", "missing", "prices", "cache", "context", "surprise", "macro"];
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
export function EventResults({ analysis, kind, focused, onFocus, chips }: { analysis: EventAnalysis; kind: EventKind; focused: string; onFocus: (symbol: string) => void; chips?: React.ReactNode }) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<string | null>(null);
  const [showBenchmark, setShowBenchmark] = useState(true);
  const [showSector, setShowSector] = useState(true);
  const [basis, setBasis] = useState<Basis>("absolute");
  const results = useMemo(() => analysis.tickers.flatMap((item) => (item.result ? [item.result] : [])), [analysis]);
  const result = results.find((item) => item.symbol === focused) ?? results[0];
  if (!result) return null;
  const benchmark = analysis.benchmark ?? null;
  const pairing = result.benchmark;
  const n = result.n;
  const selectedRow = result.rows.find((row) => row.id === selected);
  const hasContext = result.rows.some((row) => row.context.length);
  const hasExpectations = result.rows.some((row) => row.earnings);
  const select = (id: string) => {
    const owner = results.find((item) => item.rows.some((row) => row.id === id));
    if (owner && owner.symbol !== focused) onFocus(owner.symbol);
    setSelected(selected === id ? null : id);
  };
  const sectorLegend = t("ui.analysis.sector.chart_legend", { benchmark: benchmark ?? "", sector: result.sector?.symbol ?? "—" });
  const contents = [
    { id: "e-summary", label: t("ui.analysis.toc.summary") },
    { id: "e-candles", label: t("ui.analysis.toc.candles") },
    { id: "e-path", label: t("ui.analysis.toc.paths") },
    { id: "e-box", label: t("ui.analysis.toc.box") },
    { id: "e-stats", label: t("ui.analysis.toc.ranking") },
    ...(kind === "earnings" && (result.quarters?.length ?? 0) > 0 ? [{ id: "e-quarters", label: t("ui.analysis.toc.quarters") }] : []),
    ...(hasContext ? [{ id: "e-context", label: t("ui.analysis.toc.context") }] : []),
    ...(results.length > 1 ? [{ id: "e-tickers", label: t("ui.analysis.toc.tickers") }] : []),
    { id: "e-events", label: t("ui.analysis.toc.events") },
  ];
  return (
    <>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        {chips}
        <SectorNote reference={result.sector} included={result.sector_comparison_included} />
        <span className="ml-auto"><BasisChoice value={basis} onChange={setBasis} benchmark={!!benchmark} sector={!!result.sector?.symbol} /></span>
      </div>
      <section id="e-summary" className="scroll-mt-28 space-y-3">
        <ConclusionCard kind={kind} result={result} across={analysis.comparison} benchmark={benchmark} />
        <WindowCards basis={basis} result={result} selected={selected} onSelect={select} />
      </section>
      {pairing && pairing.status !== "available" && (
        <p className="rounded-md border border-warn/30 bg-warn-soft px-3 py-2 text-xs text-warn">{t("ui.analysis.benchmark_pending", { name: benchmarkName(pairing.symbol) })}</p>
      )}
      <PageContents items={contents} />
      {selectedRow && (
        <Section title={t("ui.events.kline_label", { name: selectedRow.name })} info={t(selectedRow.context.length ? "ui.events.kline_legend_context" : "ui.events.kline_legend", { n })}>
          <div className="space-y-2 rounded-lg border bg-card px-2 py-2">
            <EventKline row={selectedRow} />
            <div className="px-2"><RowContext row={selectedRow} /></div>
          </div>
        </Section>
      )}
      <Section id="e-candles" title={t("ui.events.chart_candles", { ticker: result.symbol, count: result.event_count })} info={t("ui.events.candles_legend")}>
        <div className="rounded-lg border bg-card px-2 pt-2">
          <ReactionCandles selected={selected} onSelect={select} result={result} benchmark={benchmark} title={`${result.symbol} ${t("ui.events.window.reaction")}`} />
          <p className="px-2 pb-2 text-xs text-muted-foreground">{sectorLegend}</p>
        </div>
      </Section>
      <Section
        id="e-path"
        title={t("ui.events.chart_path", { ticker: result.symbol, n })}
        info={t("ui.events.path_legend", { n })}
        actions={
          <div className="flex flex-wrap gap-3">
            {benchmark && <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground"><input type="checkbox" checked={showBenchmark} onChange={(e) => setShowBenchmark(e.target.checked)} className="accent-primary" />{t("ui.events.show_benchmark", { benchmark: benchmarkName(benchmark) })}</label>}
            {result.sector?.symbol && <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground"><input type="checkbox" checked={showSector} onChange={(e) => setShowSector(e.target.checked)} className="accent-primary" />{t("ui.events.show_sector", { sector: result.sector.symbol })}</label>}
          </div>
        }
      >
        <div className="rounded-lg border bg-card px-2 pt-2">
          <PathChart selected={selected} onSelect={select} result={result} benchmark={benchmark} showBenchmark={showBenchmark} showSector={showSector} title={t("ui.events.chart_path", { ticker: result.symbol, n })} />
          <p className="px-2 pb-2 text-xs text-muted-foreground">{sectorLegend}</p>
        </div>
      </Section>
      <FoldSection id={`events.${kind}.box`} anchor="e-box" title={t("ui.events.box.windows")} meta={<InfoTip>{t("ui.events.box.legend")}</InfoTip>}>
        <EventDistributions basis={basis} setBasis={setBasis} result={result} results={results} benchmark={benchmark} selected={selected} onSelect={select} />
      </FoldSection>
      <Section id="e-stats" title={t("ui.events.stats_title", { ticker: result.symbol })}>
        <WindowTable basis={basis} result={result} benchmark={benchmark} />
      </Section>
      {kind === "earnings" && (result.quarters?.length ?? 0) > 0 && (
        <Section id="e-quarters" title={t("ui.events.quarters_title", { ticker: result.symbol })}>
          <QuarterTable basis={basis} result={result} benchmark={benchmark} />
        </Section>
      )}
      {kind === "earnings" && hasExpectations && (
        <FoldSection id="events.expectations" title={t("ui.events.expect.title", { ticker: result.symbol })}>
          <ExpectationsTable result={result} onSelect={select} />
        </FoldSection>
      )}
      {kind === "earnings" && result.groups && (
        <FoldSection id="events.groups" title={t("ui.events.group.title", { ticker: result.symbol })}>
          <GroupStats result={result} basis={basis} selected={selected} onSelect={select} />
        </FoldSection>
      )}
      {hasContext && (
        <FoldSection id="events.timeline" anchor="e-context" title={t("ui.events.timeline.title", { ticker: result.symbol })}>
          <Timeline result={result} onSelect={select} />
        </FoldSection>
      )}
      {results.length > 1 && (
        <Section id="e-tickers" title={t("ui.events.rank_title")}>
          <TickerRankTable results={results} benchmark={benchmark} selected={result.symbol} onSelect={onFocus} />
        </Section>
      )}
      <FoldSection id={`events.${kind}.rows`} anchor="e-events" title={t("ui.events.detail_title", { ticker: result.symbol, count: result.event_count })}
        meta={<InfoTip>{t("ui.events.detail_hint")}</InfoTip>}>
        <EventDetailTable selected={selected} onSelect={select} result={result} benchmark={benchmark} />
      </FoldSection>
    </>
  );
}
