import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { CircleAlert, LoaderCircle } from "lucide-react";
import { tm } from "@/i18n";
import { benchmarkName } from "@/lib/analysis";
import { TAB, useEventAnalysis, useEventChoices, useVariant, type EventAnalysis, type EventKind, type EventFilters, SESSIONS, sessionLabel } from "@/lib/events";
import { rememberAnalysis } from "@/lib/navigation";
import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { AnalysisLayout } from "@/components/analysis/AnalysisLayout";
import { Progress } from "@/components/analysis/Progress";
import { EventInputs } from "@/components/events/EventInputs";
import { EventDataLine, EventMethod, EventResults } from "@/components/events/EventResults";

function Empty({ kind }: { kind: EventKind }) {
  const { t } = useTranslation();
  return (
    <section className="rounded-lg border border-dashed bg-card px-6 py-10 text-center">
      <h2 className="text-base font-semibold">{t(`ui.events.empty.${kind}_title`)}</h2>
      <p className="mx-auto mt-1.5 max-w-xl text-sm text-muted-foreground">{t(`ui.events.empty.${kind}_body`)}</p>
      <ol className="mx-auto mt-4 flex max-w-xl flex-wrap justify-center gap-2 text-xs text-muted-foreground">
        {(["prompt", "paste", "analyze"] as const).map((step, index) => (
          <li key={step} className="rounded-full border px-2.5 py-1">
            {index + 1}. {t(`ui.events.empty.step_${step}`)}
          </li>
        ))}
      </ol>
    </section>
  );
}

function TickerChips({ analysis, focused, onFocus }: { analysis: EventAnalysis; focused: string; onFocus: (symbol: string) => void }) {
  const steps = new Map(analysis.progress.map((item) => [item.symbol, item.step]));
  if (analysis.tickers.length < 2 || !analysis.tickers.some((item) => item.result)) return null;
  return (
    <div className="flex flex-wrap gap-1" role="tablist">
      {analysis.tickers.map(({ symbol, result }) => (
        <button
          key={symbol}
          type="button"
          role="tab"
          aria-selected={focused === symbol}
          disabled={!result}
          onClick={() => onFocus(symbol)}
          className={cn(
            "inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs font-semibold tabular-nums transition-colors",
            focused === symbol ? "border-primary bg-primary text-primary-foreground" : "bg-card hover:bg-accent",
            !result && "cursor-default font-normal text-muted-foreground hover:bg-card",
          )}
        >
          {symbol}
          {!result && steps.get(symbol) === "failed" && <CircleAlert className="size-3 text-destructive" />}
          {!result && steps.get(symbol) !== "failed" && <LoaderCircle className="size-3 motion-safe:animate-spin" />}
        </button>
      ))}
    </div>
  );
}

/**
 * Earnings and custom events: the prompt, the pasted
 * JSON and the saved sets on the left; the result of the chosen analysis on
 * the right. ?a= is the analysis, ?n= the window, ?t= the ticker shown.
 */
export function EventsPage({ kind }: { kind: EventKind }) {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const id = params.get("a");
  const update = (changes: Record<string, string | null>, replace = true) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      for (const [name, value] of Object.entries(changes)) {
        if (value == null) next.delete(name);
        else next.set(name, value);
      }
      return next;
    }, { replace });
  const choices = useEventChoices(kind);
  const [benchmark, setBenchmarkState] = useState<string | null>(choices.benchmark);
  const filters: EventFilters = {
    quarter: params.get("q") ? Number(params.get("q")) : undefined,
    recent_years: params.get("years") ? Number(params.get("years")) : undefined,
    session: (params.get("session") || undefined) as EventFilters["session"],
    direction: (params.get("direction") || undefined) as EventFilters["direction"],
  };
  const { query, refresh } = useEventAnalysis(id, (next) => update({ a: next }), filters);
  const analysis = query.data && query.data.id === id ? query.data : undefined;
  const open = (next: string) => update({ a: next, t: null }, false);
  const variant = useVariant(open);
  useEffect(() => rememberAnalysis(TAB[kind], params.toString()), [kind, params]);
  // The analysis shown decides n and the benchmark; the panel follows it.
  useEffect(() => {
    if (!analysis) return;
    if (analysis.n !== choices.n) choices.setN(analysis.n);
    setBenchmarkState(analysis.benchmark ?? null);
  }, [analysis?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const setN = (value: number | null) => {
    choices.setN(value);
    const n = value ?? choices.defaultN;
    if (analysis && n !== analysis.n) variant.mutate({ id: analysis.id, n });
  };
  const setBenchmark = (value: string | null) => {
    setBenchmarkState(value);
    choices.setBenchmark(value);
    if (analysis && value !== (analysis.benchmark ?? null)) variant.mutate({ id: analysis.id, benchmark: value });
  };
  const focused = params.get("t") ?? analysis?.tickers.find((item) => item.result)?.symbol ?? "";
  const missing = query.error && "status" in query.error && (query.error as { status?: number }).status === 404;
  const ready = analysis?.tickers.some((item) => item.result);
  return (
    <AnalysisLayout
      tab={TAB[kind]}
      conditions={
        <EventInputs
          kind={kind}
          choices={{ ...choices, setN, benchmark, setBenchmark }}
          busy={variant.isPending}
          currentSet={analysis?.event_set_id ?? null}
          onAnalysis={open}
        />
      }
    >
      {variant.error && <p className="rounded-md border border-destructive/30 bg-card px-3 py-2 text-sm text-destructive">{variant.error.message}</p>}
      {!id && <Empty kind={kind} />}
      {id && missing && <p className="rounded-md border bg-card px-3 py-2 text-sm text-muted-foreground">{t("ui.events.not_found")}</p>}
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
              {analysis.title}
              <span className="ml-2 text-base font-normal text-muted-foreground">
                {t(analysis.benchmark ? "ui.events.heading_meta" : "ui.events.heading_meta_none", { count: analysis.event_count, n: analysis.n, benchmark: benchmarkName(analysis.benchmark) })}
              </span>
            </h1>
            <EventDataLine filters={filters} analysis={analysis} refreshing={refresh.isPending} />
          </div>
          {refresh.error && <p className="text-sm text-destructive">{refresh.error.message}</p>}
          <Progress items={analysis.progress} onRetry={() => refresh.mutate(true)} retrying={refresh.isPending} />
          <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-card p-2" aria-busy={query.isFetching}>
            <span className="text-xs font-semibold">{t("ui.events.filter.title")}</span>
            <div className="inline-flex rounded border">{[0, 1, 2, 3, 4].filter((q) => kind === "earnings" || q === 0).map((q) => <button key={q} type="button" aria-pressed={(filters.quarter ?? 0) === q} onClick={() => update({ q: q ? String(q) : null })} className={cn("h-7 px-2 text-xs", (filters.quarter ?? 0) === q && "bg-primary text-primary-foreground")}>{q ? `Q${q}` : t("ui.events.filter.all")}</button>)}</div>
            <label className="text-xs">{t("ui.events.filter.years")} <input type="number" min={1} max={30} placeholder={t("ui.events.filter.all")} value={filters.recent_years ?? ""} onChange={(e) => update({ years: e.target.value || null })} className="h-7 w-16 rounded border px-1" /></label>
            <select aria-label={t("ui.events.filter.session")} value={filters.session ?? ""} onChange={(e) => update({ session: e.target.value || null })} className="h-7 rounded border bg-card px-1 text-xs"><option value="">{t("ui.events.filter.session")}</option>{SESSIONS.map((s) => <option key={s} value={s}>{sessionLabel(s)}</option>)}</select>
            <select aria-label={t("ui.events.filter.direction")} value={filters.direction ?? ""} onChange={(e) => update({ direction: e.target.value || null })} className="h-7 rounded border bg-card px-1 text-xs"><option value="">{t("ui.events.filter.direction")}</option><option value="up">{t("ui.events.filter.up")}</option><option value="down">{t("ui.events.filter.down")}</option></select>
            <button type="button" onClick={() => update({ q: null, years: null, session: null, direction: null })} className="text-xs underline">{t("ui.events.filter.reset")}</button>
            <span className="ml-auto text-xs tabular-nums text-muted-foreground">{t("ui.events.filter.count", { count: analysis.filtered_event_count, total: analysis.event_count })}</span>
            {query.isFetching && <LoaderCircle className="size-3 motion-safe:animate-spin" />}
          </div>
          <TickerChips analysis={analysis} focused={focused} onFocus={(symbol) => update({ t: symbol })} />
          {ready ? (
            <EventResults key={`${analysis.id}:${JSON.stringify(filters)}`} analysis={analysis} kind={kind} focused={focused} onFocus={(symbol) => update({ t: symbol })} />
          ) : (
            !analysis.progress.some((item) => item.step !== "failed") && (
              <p className="rounded-md border bg-card px-3 py-2 text-sm text-muted-foreground">
                {t("ui.analysis.no_results")}
                {analysis.tickers.map((item) => (item.wait_reason ? ` ${item.symbol}: ${tm(item.wait_reason)}` : "")).join("")}
              </p>
            )
          )}
          <EventMethod n={analysis.n} />
        </>
      )}
    </AnalysisLayout>
  );
}
