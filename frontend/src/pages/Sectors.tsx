import { useCallback, useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { BarChart3, CandlestickChart, ChevronRight, LayoutGrid, LoaderCircle, Table2 } from "lucide-react";
import { formatDay, formatEt } from "@/lib/format";
import {
  analysisLinks,
  INTERVALS,
  LEVELS,
  MAX_ANALYSIS_ETFS,
  parseSectorState,
  PERIODS,
  RANGES,
  rankedGroups,
  sectorSearch,
  useSectorCandles,
  useSectorGroups,
  useSectors,
  VIEWS,
  type SectorGroupItem,
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
import { SectorGroupTable } from "@/components/sectors/SectorGroupTable";
import { FetchProgress } from "@/components/sectors/SectorNotes";

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

function useSectorState(etfs: readonly string[], sectors: readonly string[]) {
  const [params, setParams] = useSearchParams();
  const state = useMemo(() => parseSectorState(params, etfs, sectors), [params, etfs, sectors]);
  /** Level changes add a history entry, so the browser's Back returns to the previous level. */
  const update = useCallback((changes: Partial<SectorState>, { push = false } = {}) => {
    setParams(new URLSearchParams(sectorSearch({ ...state, ...changes })), { replace: !push });
  }, [state, setParams]);
  return [state, update] as const;
}

/** Whether a group (its primary or a reference row) is among the selected ETFs. */
function groupSelected(item: SectorGroupItem, selected: readonly string[]) {
  return (item.etf != null && selected.includes(item.etf)) || item.alternates.some((row) => selected.includes(row.etf));
}

/**
 * The eleven S&P 500 sectors and their 25 industry groups as a table, a
 * heatmap or stacked K-lines; every choice lives in the URL.
 */
export function SectorsPage() {
  const { t } = useTranslation();
  const reduce = useReducedMotion();
  const { query, error } = useSectors({ foreground: true });
  const data = query.data;
  const etfs = useMemo(() => [...(data?.quote_symbols ?? []), ...(data?.group_etfs ?? [])], [data?.quote_symbols, data?.group_etfs]);
  const sectorIds = useMemo(() => data?.sector_ids ?? [], [data?.sector_ids]);
  const [state, update] = useSectorState(etfs, sectorIds);
  const groupLevel = state.level === "group";
  const groups = useSectorGroups(state.sector, groupLevel);
  const groupData = groups.data;
  const candles = useSectorCandles(state.range, state.interval, state.view === "kline", state.level, state.sector);
  const items = useMemo(() => (data?.items ?? []).filter((item) => !state.onlySelected || state.selected.includes(item.etf)), [data, state.onlySelected, state.selected]);
  const groupItems = useMemo(() => (groupData?.items ?? []).filter((item) => !state.onlySelected || groupSelected(item, state.selected)), [groupData, state.onlySelected, state.selected]);
  const toggle = useCallback((etf: string) => {
    const selected = state.selected.includes(etf) ? state.selected.filter((value) => value !== etf) : [...state.selected, etf];
    update({ selected, onlySelected: state.onlySelected && selected.length > 0 });
  }, [state.selected, state.onlySelected, update]);
  const setSort = useCallback((sort: SortColumn, desc: boolean) => update({ sort, desc }), [update]);
  const drill = useCallback((sector: string | null) => update({ level: "group", sector }, { push: true }), [update]);
  const sectorName = useCallback((id: string) => t(`ui.sectors.name.${id}`), [t]);
  const groupName = useCallback((id: string) => t(`ui.sectors.group.${id}`), [t]);
  // The ETFs of the level on screen: the sectors', or the ranked groups' primaries.
  const levelEtfs = groupLevel ? rankedGroups(groupData?.items ?? []).map((item) => item.etf) : (data?.items ?? []).map((item) => item.etf);
  const elsewhere = state.selected.filter((etf) => !levelEtfs.includes(etf));
  const analyzed = state.selected.length ? state.selected : levelEtfs;
  const tooMany = analyzed.length > MAX_ANALYSIS_ETFS;
  const links = analysisLinks(analyzed);
  const pending = groupLevel ? [] : data?.prices_pending ?? [];
  const fetchRows = groupData ? groupData.items.flatMap((item) => [...(item.etf ? [{ etf: item.etf, fetch: item.fetch }] : []), ...item.alternates]) : [];
  const shown = groupLevel ? groupData : data;
  const levelLabel = t(`ui.sectors.level.${state.level}`);

  return (
    <div className="space-y-4">
      <header className="space-y-1">
        <nav aria-label={t("ui.sectors.breadcrumb")} className="flex flex-wrap items-center gap-1 text-xl font-semibold">
          {groupLevel
            ? <button type="button" onClick={() => update({ level: "sector", sector: null }, { push: true })} className="text-muted-foreground hover:text-foreground hover:underline">{t("ui.sectors.title")}</button>
            : <h1>{t("ui.sectors.title")}</h1>}
          {groupLevel && <>
            <ChevronRight className="size-4 text-muted-foreground" aria-hidden/>
            <h1>{state.sector ? sectorName(state.sector) : t("ui.sectors.all_groups")}</h1>
          </>}
        </nav>
        <p className="text-sm text-muted-foreground">{t(groupLevel ? "ui.sectors.group_scope" : "ui.sectors.scope")}</p>
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground" role="status">
          {shown ? <TodayBasis data={shown}/> : <Skeleton className="h-3 w-48"/>}
          {shown && <span>{t("ui.sectors.periods_through", { day: formatDay(shown.last_completed_session, { year: true }) })}</span>}
          {pending.length > 0 && (
            <span className="inline-flex items-center gap-1.5">
              <LoaderCircle className="size-3 motion-safe:animate-spin" aria-hidden/>
              {t("ui.sectors.fetching", { count: pending.length })}
            </span>
          )}
          {shown && <span>{t("ui.sectors.read_at", { time: formatEt(shown.as_of) })}</span>}
          {(groupLevel ? groups.error : error) && <span className="text-destructive">{(groupLevel ? groups.error : error)?.message}</span>}
        </p>
        {groupLevel && <FetchProgress rows={fetchRows}/>}
      </header>

      <div className="flex flex-wrap items-center gap-3">
        <Segmented label={t("ui.sectors.level_label")} value={state.level} options={LEVELS}
          text={(level) => t(`ui.sectors.level.${level}`)}
          choose={(level) => update({ level, sector: level === "group" ? state.sector : null }, { push: true })}/>
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

      {groupLevel && (
        <div className="flex flex-wrap items-center gap-1 text-xs" role="group" aria-label={t("ui.sectors.choose_sector")}>
          {[null, ...sectorIds].map((id) => (
            <button key={id ?? "all"} type="button" aria-pressed={state.sector === id} onClick={() => update({ sector: id }, { push: true })}
              className={cn("rounded border px-2 py-0.5 transition-colors hover:bg-accent",
                state.sector === id && "border-foreground bg-foreground text-background hover:bg-foreground/90")}>
              {id ? t(`ui.sectors.short.${id}`) : t("ui.sectors.all_groups")}
            </button>
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs">
        <span className="text-muted-foreground">{t("ui.sectors.selection")}</span>
        <div className="flex flex-wrap gap-1" role="group" aria-label={t("ui.sectors.select")}>
          {levelEtfs.map((etf) => (
            <button key={etf} type="button" aria-pressed={state.selected.includes(etf)} onClick={() => toggle(etf)}
              className={cn("rounded border px-1.5 py-0.5 font-mono transition-colors hover:bg-accent",
                state.selected.includes(etf) && "border-foreground bg-foreground text-background hover:bg-foreground/90")}>
              {etf}
            </button>
          ))}
        </div>
        {elsewhere.length > 0 && (
          <span className="flex flex-wrap items-center gap-1">
            <span className="text-muted-foreground">{t("ui.sectors.selected_elsewhere")}</span>
            {elsewhere.map((etf) => (
              <button key={etf} type="button" onClick={() => toggle(etf)} aria-label={t("ui.sectors.unselect", { etf })}
                className="rounded border border-foreground bg-foreground px-1.5 py-0.5 font-mono text-background hover:bg-foreground/90">
                {etf} ×
              </button>
            ))}
          </span>
        )}
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
          {tooMany
            ? <span className="text-warn" role="alert">{t("ui.sectors.too_many", { count: analyzed.length, max: MAX_ANALYSIS_ETFS })}</span>
            : <>
              <span className="text-muted-foreground">{t(state.selected.length ? "ui.sectors.analyze_selected" : "ui.sectors.analyze_all", { count: analyzed.length })}</span>
              <Link to={links.monthly} className="font-medium hover:underline">{t("ui.sectors.monthly")}</Link>
              <Link to={links.interval} className="font-medium hover:underline">{t("ui.sectors.interval")}</Link>
            </>}
        </span>
      </div>

      <AnimatePresence mode="wait" initial={false}>
        <motion.div key={`${state.level}-${state.sector ?? ""}-${state.view}`}
          initial={reduce ? { opacity: 0 } : { opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          transition={{ duration: reduce ? 0.1 : 0.2, ease: "easeOut" }}>
          {!shown ? (
            <div className="space-y-2 rounded-lg border bg-card p-3">
              {Array.from({ length: 11 }, (_, index) => <Skeleton key={index} className="h-7"/>)}
            </div>
          ) : groupLevel ? (
            state.view === "table" ? (
              <SectorGroupTable items={groupItems} historyYears={shown.history_years} sort={state.sort} desc={state.desc}
                setSort={setSort} selected={state.selected} toggle={toggle} onlySelected={state.onlySelected} showSector={!state.sector}/>
            ) : state.view === "heatmap" ? (
              <SectorHeatmap items={rankedGroups(groupItems)} period={state.period} onSelect={toggle} name={groupName} label={levelLabel}/>
            ) : (
              <SectorCharts items={rankedGroups(groupItems)} data={candles.data} loading={candles.isPending} name={groupName}/>
            )
          ) : state.view === "table" ? (
            <SectorTable items={items} historyYears={shown.history_years} sort={state.sort} desc={state.desc}
              setSort={setSort} selected={state.selected} toggle={toggle} drill={drill}/>
          ) : state.view === "heatmap" ? (
            <SectorHeatmap items={items} period={state.period} onSelect={toggle} name={sectorName} label={levelLabel}/>
          ) : (
            <SectorCharts items={items} data={candles.data} loading={candles.isPending} name={sectorName}/>
          )}
        </motion.div>
      </AnimatePresence>
      {state.view === "heatmap" && <p className="text-xs text-muted-foreground">{t("ui.sectors.heatmap_rule")}</p>}
      {state.view === "kline" && candles.data && (
        <p className="text-xs text-muted-foreground">{t("ui.sectors.chart_rule", { start: formatDay(candles.data.start_date, { year: true }), end: formatDay(candles.data.end_date, { year: true }) })}</p>
      )}
      {groupLevel && <p className="text-xs text-muted-foreground">{t("ui.sectors.group_method")}</p>}
      <p className="text-xs text-muted-foreground">{t("ui.sectors.method")}</p>
    </div>
  );
}
