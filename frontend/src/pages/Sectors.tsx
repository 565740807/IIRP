import { useCallback, useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { BarChart3, CandlestickChart, LayoutGrid, LoaderCircle, Table2 } from "lucide-react";
import { formatDay, formatEt } from "@/lib/format";
import {
  analysisLinks,
  INTERVALS,
  parseSectorState,
  PERIODS,
  RANGES,
  sectorSearch,
  useSectorCandles,
  useSectors,
  VIEWS,
  type SectorState,
  type SortColumn,
} from "@/lib/sectors";
import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { TodayBasis } from "@/components/home/SectorStrip";
import { SectorCharts } from "@/components/sectors/SectorCharts";
import { SectorHeatmap } from "@/components/sectors/SectorHeatmap";
import { SectorTable } from "@/components/sectors/SectorTable";

const VIEW_ICONS = { table: Table2, heatmap: LayoutGrid, kline: CandlestickChart } as const;

function Segmented<T extends string>({ label, value, options, text, choose }: {
  label: string; value: T; options: readonly T[]; text: (value: T) => string; choose: (value: T) => void;
}) {
  return (
    <ToggleGroup type="single" size="sm" variant="outline" spacing={0} value={value} aria-label={label}
      onValueChange={(next) => { if (next) choose(next as T); }}>
      {options.map((option) => <ToggleGroupItem key={option} value={option} className="px-2.5">{text(option)}</ToggleGroupItem>)}
    </ToggleGroup>
  );
}

function useSectorState(etfs: readonly string[]) {
  const [params, setParams] = useSearchParams();
  const state = useMemo(() => parseSectorState(params, etfs), [params, etfs]);
  const update = useCallback((changes: Partial<SectorState>) => {
    setParams(new URLSearchParams(sectorSearch({ ...state, ...changes })), { replace: true });
  }, [state, setParams]);
  return [state, update] as const;
}

/** The eleven S&P 500 sectors as a table, a heatmap or stacked K-lines; every choice lives in the URL. */
export function SectorsPage() {
  const { t } = useTranslation();
  const reduce = useReducedMotion();
  const { query, error } = useSectors({ foreground: true });
  const data = query.data;
  const etfs = useMemo(() => data?.quote_symbols ?? [], [data?.quote_symbols]);
  const [state, update] = useSectorState(etfs);
  const candles = useSectorCandles(state.range, state.interval, state.view === "kline");
  const items = useMemo(() => (data?.items ?? []).filter((item) => !state.onlySelected || state.selected.includes(item.etf)), [data, state.onlySelected, state.selected]);
  const toggle = useCallback((etf: string) => {
    const selected = state.selected.includes(etf) ? state.selected.filter((value) => value !== etf) : [...state.selected, etf];
    update({ selected, onlySelected: state.onlySelected && selected.length > 0 });
  }, [state.selected, state.onlySelected, update]);
  const setSort = useCallback((sort: SortColumn, desc: boolean) => update({ sort, desc }), [update]);
  const links = analysisLinks(state.selected.length ? state.selected : etfs);
  const pending = data?.prices_pending ?? [];

  return (
    <div className="space-y-4">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold">{t("ui.sectors.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("ui.sectors.scope")}</p>
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground" role="status">
          {data ? <TodayBasis data={data}/> : <Skeleton className="h-3 w-48"/>}
          {data && <span>{t("ui.sectors.periods_through", { day: formatDay(data.last_completed_session, { year: true }) })}</span>}
          {pending.length > 0 && (
            <span className="inline-flex items-center gap-1.5">
              <LoaderCircle className="size-3 motion-safe:animate-spin" aria-hidden/>
              {t("ui.sectors.fetching", { count: pending.length })}
            </span>
          )}
          {data && <span>{t("ui.sectors.read_at", { time: formatEt(data.as_of) })}</span>}
          {error && <span className="text-destructive">{error.message}</span>}
        </p>
      </header>

      <div className="flex flex-wrap items-center gap-3">
        <ToggleGroup type="single" size="sm" variant="outline" spacing={0} value={state.view} aria-label={t("ui.sectors.view_label")}
          onValueChange={(view) => { if (view) update({ view: view as SectorState["view"] }); }}>
          {VIEWS.map((view) => {
            const Icon = VIEW_ICONS[view];
            return <ToggleGroupItem key={view} value={view} className="gap-1.5 px-2.5"><Icon className="size-3.5"/>{t(`ui.sectors.view.${view}`)}</ToggleGroupItem>;
          })}
        </ToggleGroup>
        {state.view === "heatmap" && (
          <Segmented label={t("ui.sectors.period_label")} value={state.period} options={PERIODS}
            text={(period) => t(`ui.sectors.period.${period}`)} choose={(period) => update({ period })}/>
        )}
        {state.view === "kline" && <>
          <Segmented label={t("ui.sectors.range_label")} value={state.range} options={RANGES}
            text={(range) => t(`ui.sectors.range.${range}`)} choose={(range) => update({ range })}/>
          <Segmented label={t("ui.sectors.interval_label")} value={state.interval} options={INTERVALS}
            text={(interval) => t(`ui.sectors.interval.${interval}`)} choose={(interval) => update({ interval })}/>
        </>}
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs">
        <span className="text-muted-foreground">{t("ui.sectors.selection")}</span>
        <div className="flex flex-wrap gap-1" role="group" aria-label={t("ui.sectors.select")}>
          {(data?.items ?? []).map((item) => (
            <button key={item.etf} type="button" aria-pressed={state.selected.includes(item.etf)} onClick={() => toggle(item.etf)}
              title={t(`ui.sectors.name.${item.id}`)}
              className={cn("rounded border px-1.5 py-0.5 font-mono transition-colors hover:bg-accent",
                state.selected.includes(item.etf) && "border-foreground bg-foreground text-background hover:bg-foreground/90")}>
              {item.etf}
            </button>
          ))}
        </div>
        <label className={cn("flex items-center gap-1.5", !state.selected.length && "opacity-50")}>
          <input type="checkbox" disabled={!state.selected.length} checked={state.onlySelected}
            onChange={(event) => update({ onlySelected: event.target.checked })}/>
          {t("ui.sectors.only_selected")}
        </label>
        {state.selected.length > 0 && (
          <button type="button" className="text-muted-foreground hover:text-foreground hover:underline" onClick={() => update({ selected: [], onlySelected: false })}>
            {t("ui.sectors.clear")}
          </button>
        )}
        <span className="ml-auto flex flex-wrap items-center gap-2">
          <BarChart3 className="size-3.5 text-muted-foreground" aria-hidden/>
          <span className="text-muted-foreground">{t(state.selected.length ? "ui.sectors.analyze_selected" : "ui.sectors.analyze_all", { count: state.selected.length })}</span>
          <Link to={links.monthly} className="font-medium hover:underline">{t("ui.sectors.monthly")}</Link>
          <Link to={links.interval} className="font-medium hover:underline">{t("ui.sectors.interval")}</Link>
        </span>
      </div>

      <AnimatePresence mode="wait" initial={false}>
        <motion.div key={state.view}
          initial={reduce ? { opacity: 0 } : { opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          transition={{ duration: reduce ? 0.1 : 0.2, ease: "easeOut" }}>
          {!data ? (
            <div className="space-y-2 rounded-lg border bg-card p-3">
              {Array.from({ length: 11 }, (_, index) => <Skeleton key={index} className="h-7"/>)}
            </div>
          ) : state.view === "table" ? (
            <SectorTable items={items} historyYears={data.history_years} sort={state.sort} desc={state.desc}
              setSort={setSort} selected={state.selected} toggle={toggle}/>
          ) : state.view === "heatmap" ? (
            <SectorHeatmap items={items} period={state.period} onSelect={toggle}/>
          ) : (
            <SectorCharts items={items} data={candles.data} loading={candles.isPending}/>
          )}
        </motion.div>
      </AnimatePresence>
      {state.view === "heatmap" && <p className="text-xs text-muted-foreground">{t("ui.sectors.heatmap_rule")}</p>}
      {state.view === "kline" && candles.data && (
        <p className="text-xs text-muted-foreground">{t("ui.sectors.chart_rule", { start: formatDay(candles.data.start_date, { year: true }), end: formatDay(candles.data.end_date, { year: true }) })}</p>
      )}
      <p className="text-xs text-muted-foreground">{t("ui.sectors.method")}</p>
    </div>
  );
}
