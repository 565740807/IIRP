import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { CircleAlert, Download, LoaderCircle } from "lucide-react";
import { tm } from "@/i18n";
import {
  exportHref,
  monthName,
  parseTickers,
  periodName,
  useAnalysis,
  useCreateAnalysis,
  type Analysis,
  type SeasonalKind,
  type TickerResult,
} from "@/lib/analysis";
import { formatDay, formatEt, formatLocal } from "@/lib/format";
import { rememberAnalysis } from "@/lib/navigation";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { AnalysisLayout } from "@/components/analysis/AnalysisLayout";
import { Conditions, DEFAULT_CONDITIONS, type ConditionValues } from "@/components/analysis/Conditions";
import { MonthGrid } from "@/components/analysis/MonthGrid";
import { Progress } from "@/components/analysis/Progress";
import { ConclusionCard, Method } from "@/components/analysis/Summary";
import { DetailTable, MonthTable, TickerTable } from "@/components/analysis/Tables";
import { YearCandles } from "@/components/analysis/YearCandles";

const DRAFT = "iirp.analysis.conditions";

function savedConditions(kind: SeasonalKind): ConditionValues {
  try {
    const value = JSON.parse(localStorage.getItem(`${DRAFT}.${kind}`) ?? "null");
    if (value && Array.isArray(value.tickers)) return { ...DEFAULT_CONDITIONS, ...value };
  } catch {
    /* Defaults. */
  }
  return DEFAULT_CONDITIONS;
}

function saveConditions(kind: SeasonalKind, values: ConditionValues) {
  try {
    localStorage.setItem(`${DRAFT}.${kind}`, JSON.stringify(values));
  } catch {
    /* Optional convenience. */
  }
}

/** The conditions a research was run with, for the form. */
function conditionsOf(analysis: Analysis): ConditionValues {
  const params = analysis.params as Record<string, unknown>;
  return {
    tickers: (params.tickers as string[]) ?? [],
    years: Number(params.historical_years ?? DEFAULT_CONDITIONS.years),
    benchmark: (params.benchmark as string | null | undefined) ?? null,
    start: (params.start_mmdd as string) ?? DEFAULT_CONDITIONS.start,
    end: (params.end_mmdd as string) ?? DEFAULT_CONDITIONS.end,
  };
}

function Section({ title, actions, children, className }: { title: React.ReactNode; actions?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={cn("space-y-2", className)}>
      <header className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">{title}</h2>
        {actions && <div className="ml-auto flex items-center gap-1.5">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

function Empty({ kind }: { kind: SeasonalKind }) {
  const { t } = useTranslation();
  return (
    <section className="rounded-lg border border-dashed bg-card px-6 py-10 text-center">
      <h2 className="text-base font-semibold">{t(`ui.analysis.empty.${kind}_title`)}</h2>
      <p className="mx-auto mt-1.5 max-w-xl text-sm text-muted-foreground">{t(`ui.analysis.empty.${kind}_body`)}</p>
    </section>
  );
}

/** Fetched/expiry times (D14) and the CSV exports that keep a result after 24 hours. */
function DataLine({ analysis, refreshing }: { analysis: Analysis; refreshing: boolean }) {
  const { t } = useTranslation();
  const fresh = analysis.freshness;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
      {refreshing || fresh?.expired ? (
        <span className="inline-flex items-center gap-1.5">
          <LoaderCircle className="size-3.5 motion-safe:animate-spin" />
          {t("ui.analysis.expired_refetching")}
        </span>
      ) : fresh?.price_fetched_at ? (
        <span title={t("ui.time.local", { time: formatLocal(fresh.price_fetched_at) })}>
          {t("ui.analysis.fetched", { fetched: formatEt(fresh.price_fetched_at), expires: formatEt(fresh.price_expires_at) })}
        </span>
      ) : null}
      {fresh?.research_cutoff && <span>{t("ui.analysis.cutoff", { day: formatDay(fresh.research_cutoff, { year: true }) })}</span>}
      {analysis.results.length > 0 && (
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

function TickerChips({ analysis, focused, onFocus }: { analysis: Analysis; focused: string; onFocus: (symbol: string) => void }) {
  const steps = new Map((analysis.progress ?? []).map((item) => [item.symbol, item.step]));
  const symbols = (analysis.params.tickers as string[] | undefined) ?? analysis.results.map((item) => item.symbol);
  if (symbols.length < 2 || !analysis.results.length) return null;
  return (
    <div className="flex flex-wrap gap-1" role="tablist">
      {symbols.map((symbol) => {
        const ready = analysis.results.some((item) => item.symbol === symbol);
        const step = steps.get(symbol);
        return (
          <button
            key={symbol}
            type="button"
            role="tab"
            aria-selected={focused === symbol}
            disabled={!ready}
            onClick={() => onFocus(symbol)}
            className={cn(
              "inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs font-semibold tabular-nums transition-colors",
              focused === symbol ? "border-primary bg-primary text-primary-foreground" : "bg-card hover:bg-accent",
              !ready && "cursor-default font-normal text-muted-foreground hover:bg-card",
            )}
          >
            {symbol}
            {!ready && step === "failed" && <CircleAlert className="size-3 text-destructive" />}
            {!ready && step !== "failed" && <LoaderCircle className="size-3 motion-safe:animate-spin" />}
          </button>
        );
      })}
    </div>
  );
}

function Results({ kind, analysis, month, setMonth, focused, setFocused, view, setView }: {
  kind: SeasonalKind;
  analysis: Analysis;
  month: number;
  setMonth: (month: number) => void;
  focused: string;
  setFocused: (symbol: string) => void;
  view: "months" | "tickers";
  setView: (view: "months" | "tickers") => void;
}) {
  const { t } = useTranslation();
  const result: TickerResult | undefined = analysis.results.find((item) => item.symbol === focused) ?? analysis.results[0];
  const benchmark = (analysis.params.benchmark as string | null | undefined) ?? null;
  const key = kind === "monthly" ? String(month) : "interval";
  const pick = (item: TickerResult) => item.data.periods.find((period) => period.key === key)!;
  const others = useMemo(() => analysis.results.map((item) => ({ symbol: item.symbol, period: pick(item) })), [analysis.results, key]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!result) return null;
  const period = pick(result);
  const multiple = analysis.results.length > 1;
  const name = periodName(period);
  const pairing = result.data.metadata.benchmark;
  return (
    <>
      <ConclusionCard symbol={result.symbol} period={period} benchmark={benchmark} others={others} />
      {pairing && pairing.status !== "available" && (
        <p className="rounded-md border border-warn/30 bg-warn-soft px-3 py-2 text-xs text-warn">{t("ui.analysis.benchmark_pending", { name: pairing.symbol })}</p>
      )}
      {kind === "monthly" && (
        <Section title={t("ui.analysis.grid_title", { ticker: result.symbol })}>
          <MonthGrid periods={result.data.periods} selected={month} onSelect={setMonth} />
        </Section>
      )}
      <Section title={t("ui.analysis.chart_title", { ticker: result.symbol, period: name })}>
        <div className="rounded-lg border bg-card px-2 pt-2">
          <YearCandles candles={period.years} cross={period.cross_year} benchmark={benchmark} title={`${result.symbol} ${name}`} />
          <p className="px-2 pb-2 text-xs text-muted-foreground">{t(benchmark ? "ui.analysis.chart_legend_benchmark" : "ui.analysis.chart_legend", { benchmark: benchmark ?? "" })}</p>
        </div>
      </Section>
      <Section
        title={kind === "monthly" && view === "months" ? t("ui.analysis.rank_months", { ticker: result.symbol }) : t("ui.analysis.rank_tickers", { period: name })}
        actions={
          kind === "monthly" && multiple ? (
            <div className="inline-flex overflow-hidden rounded-md border text-xs">
              {(["months", "tickers"] as const).map((value) => (
                <button key={value} type="button" aria-pressed={view === value} onClick={() => setView(value)}
                  className={cn("h-7 border-r px-2.5 last:border-r-0 hover:bg-accent", view === value && "bg-primary text-primary-foreground hover:bg-primary")}>
                  {value === "months" ? t("ui.analysis.view_months") : t("ui.analysis.view_tickers", { month: monthName(month) })}
                </button>
              ))}
            </div>
          ) : undefined
        }
      >
        {kind === "monthly" && view === "months" ? (
          <MonthTable periods={result.data.periods} benchmark={benchmark} selected={month} onSelect={setMonth} />
        ) : (
          <TickerTable rows={others} benchmark={benchmark} selected={result.symbol} onSelect={setFocused} />
        )}
      </Section>
      <Section title={t("ui.analysis.detail_title", { ticker: result.symbol, period: name })}>
        <DetailTable period={period} benchmark={benchmark} />
      </Section>
    </>
  );
}

/**
 * Monthly (D6: a month's first session open → last session close) and interval
 * research (start date open → end date close, across the year end if needed).
 */
export function SeasonalPage({ kind }: { kind: SeasonalKind }) {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const id = params.get("id");
  const month = Math.min(12, Math.max(1, Number(params.get("m")) || new Date().getMonth() + 1));
  const update = (changes: Record<string, string | null>, replace = true) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      for (const [name, value] of Object.entries(changes)) {
        if (value == null) next.delete(name);
        else next.set(name, value);
      }
      return next;
    }, { replace });
  const { query, refresh } = useAnalysis(id, (next) => update({ id: next }));
  const create = useCreateAnalysis((next) => update({ id: next, t: null }, false));
  const analysis = query.data && query.data.id === id ? query.data : undefined;
  useEffect(() => rememberAnalysis(kind, params.toString()), [kind, params]);
  // A link may name tickers to start from (?tickers=AAPL,MSFT).
  const [draft, setDraft] = useState(() => {
    const saved = savedConditions(kind);
    const named = params.get("tickers");
    return named ? { ...saved, tickers: parseTickers(named) } : saved;
  });
  const initial = analysis ? conditionsOf(analysis) : draft;
  const focused = params.get("t") ?? analysis?.results[0]?.symbol ?? "";
  const view = params.get("view") === "tickers" ? "tickers" : "months";
  const run = (values: ConditionValues) => {
    saveConditions(kind, values);
    setDraft(values);
    create.mutate({
      request_id: crypto.randomUUID(),
      kind,
      tickers: values.tickers,
      historical_years: values.years,
      benchmark: values.benchmark,
      start_mmdd: values.start,
      end_mmdd: values.end,
    });
  };
  const missing = query.error && "status" in query.error && (query.error as { status?: number }).status === 404;
  return (
    <AnalysisLayout tab={kind} conditions={<Conditions kind={kind} initial={initial} running={create.isPending} onRun={run} />}>
      {create.error && <p className="rounded-md border border-destructive/30 bg-card px-3 py-2 text-sm text-destructive">{create.error.message}</p>}
      {!id && <Empty kind={kind} />}
      {id && missing && <p className="rounded-md border bg-card px-3 py-2 text-sm text-muted-foreground">{t("ui.analysis.not_found")}</p>}
      {id && query.error && !missing && <p className="text-sm text-destructive">{query.error.message}</p>}
      {id && !analysis && !query.error && (
        <div className="space-y-3">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-64 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      )}
      {analysis && (
        <>
          <div className="space-y-1">
            <h1 className="text-lg font-semibold tracking-tight">
              {t(`ui.analysis.heading.${kind}`)}
              <span className="ml-2 text-base font-normal text-muted-foreground">{(analysis.params.tickers as string[]).join(", ")}</span>
            </h1>
            <DataLine analysis={analysis} refreshing={refresh.isPending} />
          </div>
          {refresh.error && <p className="text-sm text-destructive">{refresh.error.message}</p>}
          <Progress items={analysis.progress ?? []} onRetry={() => refresh.mutate(true)} retrying={refresh.isPending} />
          <TickerChips analysis={analysis} focused={focused} onFocus={(symbol) => update({ t: symbol })} />
          {analysis.results.length > 0 ? (
            <Results
              kind={kind}
              analysis={analysis}
              month={month}
              setMonth={(value) => update({ m: String(value) })}
              focused={focused}
              setFocused={(symbol) => update({ t: symbol })}
              view={view}
              setView={(value) => update({ view: value === "tickers" ? value : null })}
            />
          ) : (
            !(analysis.progress ?? []).some((item) => item.step !== "failed") && (
              <p className="rounded-md border bg-card px-3 py-2 text-sm text-muted-foreground">
                {t("ui.analysis.no_results")}
                {analysis.batch.items.map((item) => (item.wait_reason ? ` ${item.symbol}: ${tm(item.wait_reason)}` : "")).join("")}
              </p>
            )
          )}
          <Method kind={kind} />
        </>
      )}
    </AnalysisLayout>
  );
}
