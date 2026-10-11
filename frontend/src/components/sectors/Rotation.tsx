import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { createColumnHelper } from "@tanstack/react-table";
import { motion, useReducedMotion } from "motion/react";
import { ArrowDown, ArrowUp, BarChart3, LoaderCircle } from "lucide-react";
import i18n from "@/i18n";
import { EMPTY_DISTRIBUTION, monthDay, monthName, pct } from "@/lib/analysis";
import { formatDay, formatEt } from "@/lib/format";
import {
  rankedValue,
  rotationName,
  yearValue,
  type Rotation,
  type RotationDetail,
  type RotationLeader,
  type RotationRow,
} from "@/lib/rotation";
import { rotationAnalysisLink, type Level, type RankBasis } from "@/lib/sectorState";
import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { BoxPlot, type BoxGroup } from "@/components/analysis/BoxPlot";
import { tint } from "@/components/analysis/MonthGrid";
import { Signed, SortableTable, type Meta } from "@/components/analysis/Tables";
import { UpShare } from "@/components/analysis/UpShare";

type Choice = { level: Level; rank: RankBasis; years: number };

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <div>
        <h2 className="text-sm font-semibold">{title}</h2>
        {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      </div>
      {children}
    </section>
  );
}

/** "XLK · Tech": the ETF, then the sector's short name or the group's name. */
function useLabel(level: Level) {
  return (id: string, etf: string) => `${etf} · ${rotationName(level, id, true)}`;
}

/** Name of the period: a month, or the interval's month-days. */
export function rotationPeriodName(period: { month?: number | null; start_mmdd: string; end_mmdd: string }) {
  return period.month ? monthName(period.month) : `${monthDay(period.start_mmdd)} → ${monthDay(period.end_mmdd)}`;
}

/** Spinner and placeholders while the backend ranks the ETFs from local prices. */
export function RotationLoading() {
  const { t } = useTranslation();
  return (
    <div className="space-y-3" role="status" aria-live="polite">
      <p className="inline-flex items-center gap-2 text-sm text-muted-foreground">
        <LoaderCircle className="size-4 motion-safe:animate-spin" aria-hidden />
        {t("ui.rotation.loading")}
      </p>
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-72 w-full" />
      <Skeleton className="h-48 w-full" />
    </div>
  );
}

/**
 * One sentence on the chosen period: the first and the last ETF by the ranked
 * basis, with the backend's median (or median excess) and up (or beat) count.
 */
export function RotationSummary({ data, detail, choice }: { data: Rotation; detail: RotationDetail; choice: Choice }) {
  const { t } = useTranslation();
  const label = useLabel(choice.level);
  const best = detail.rows.find((row) => row.id === detail.best);
  const worst = detail.rows.find((row) => row.id === detail.worst);
  const period = rotationPeriodName(detail);
  if (!best?.stats) return <p className="text-base font-medium">{t("ui.rotation.summary_empty", { period })}</p>;
  const relative = choice.rank === "relative";
  return (
    <p className="text-base leading-relaxed font-medium text-balance">
      {t(relative ? "ui.rotation.summary_relative" : "ui.rotation.summary", {
        period, years: data.years,
        best: label(best.id, best.etf), best_value: pct(rankedValue(best.stats, choice.rank)),
        up: relative ? best.stats.beat : best.stats.up, n: relative ? best.stats.paired_n : best.stats.n,
        worst: worst ? label(worst.id, worst.etf) : "—",
        worst_value: worst?.stats ? pct(rankedValue(worst.stats, choice.rank)) : "—",
      })}
    </p>
  );
}

/** When and from what the ranks were computed, and what is missing. */
export function RotationFacts({ data }: { data: Rotation }) {
  const { t } = useTranslation();
  const missing = data.items.filter((item) => item.status === "no_prices").map((item) => item.etf);
  // The local history may begin within the first counted year; months before it have no value.
  const starts = data.items.map((item) => item.first_date).filter((day): day is string => day != null).sort();
  const late = starts.length > 0 && starts[0] > `${data.historical_years[0]}-01-31` ? starts[0] : null;
  return (
    <p className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
      <span>{t("ui.rotation.years_span", { first: data.historical_years[0], last: data.historical_years.at(-1), current: data.current_year })}</span>
      <span>{t("ui.rotation.computed", { time: formatEt(data.computed_at), day: formatDay(data.cutoff_date, { year: true }) })}</span>
      {late && <span>{t("ui.rotation.local_start", { day: formatDay(late, { year: true }) })}</span>}
      {missing.length > 0 && <span className="text-warn">{t("ui.rotation.no_prices", { etfs: missing.join(", ") })}</span>}
      {data.benchmark_status !== "available" && <span className="text-warn">{t("ui.rotation.no_benchmark")}</span>}
    </p>
  );
}

/**
 * Month × sector table: each cell is the median (or median excess) of the
 * complete past years and its rank that month. This month's column is
 * highlighted; clicking a month shows it in full below.
 */
export function RotationHeat({ data, choice, selected, onSelect }: {
  data: Rotation; choice: Choice; selected: number; onSelect: (month: number) => void;
}) {
  const { t } = useTranslation();
  const ranked = data.items.length;
  return (
    <div className="overflow-x-auto rounded-lg border bg-card">
      <table className="w-full min-w-[960px] table-fixed border-collapse text-xs tabular-nums">
        <colgroup>
          <col className={choice.level === "group" ? "w-52" : "w-44"} />
          {data.periods.map((period) => <col key={period.key} />)}
        </colgroup>
        <thead>
          <tr className="border-b">
            <th className="px-2 py-1.5 text-left font-medium text-muted-foreground">{t(`ui.sectors.level.${choice.level}`)}</th>
            {data.periods.map((period) => {
              const current = period.month === data.current_month;
              return (
                <th key={period.key} className={cn("p-0 font-medium", current && "bg-primary/10")}>
                  <button type="button" onClick={() => onSelect(period.month!)} aria-pressed={selected === period.month}
                    className={cn("flex w-full flex-col items-center px-1 py-1 text-muted-foreground hover:text-foreground",
                      selected === period.month && "bg-primary text-primary-foreground hover:text-primary-foreground")}>
                    <span>{monthName(period.month!, "short")}</span>
                    {current && <span className="text-[9px] leading-none font-normal">{t("ui.rotation.this_month")}</span>}
                  </button>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {data.items.map((item) => (
            <tr key={item.id} className="border-b border-border/60 last:border-b-0">
              <th className="px-2 py-1 text-left font-normal">
                <span className="flex items-center gap-1.5">
                  <span className="w-11 shrink-0 font-mono text-[11px] text-muted-foreground">{item.etf}</span>
                  <span className="line-clamp-2 leading-tight font-medium" title={rotationName(choice.level, item.id)}>{rotationName(choice.level, item.id, true)}</span>
                  <Link to={rotationAnalysisLink(item.etf, data.years, { month: selected })} title={t("ui.rotation.open_monthly", { etf: item.etf })}
                    aria-label={t("ui.rotation.open_monthly", { etf: item.etf })} className="ml-auto shrink-0 text-muted-foreground hover:text-foreground">
                    <BarChart3 className="size-3.5" />
                  </Link>
                </span>
              </th>
              {data.periods.map((period) => {
                const cell = period.cells.find((entry) => entry.id === item.id);
                const value = rankedValue(cell, choice.rank);
                const current = period.month === data.current_month;
                return (
                  <td key={period.key} className={cn("p-0", current && "bg-primary/5",
                    selected === period.month && "outline outline-1 -outline-offset-1 outline-primary/50")}>
                    <button type="button" onClick={() => onSelect(period.month!)}
                      title={cell?.n ? t("ui.rotation.cell_tip", {
                        name: rotationName(choice.level, item.id), month: monthName(period.month!), value: pct(value, 2),
                        rank: cell.rank ?? "—", total: ranked, up: cell.up, n: cell.n,
                      }) : t("ui.rotation.cell_empty")}
                      className={cn("flex h-9 w-full flex-col items-center justify-center leading-tight hover:ring-1 hover:ring-ring",
                        value == null && "text-muted-foreground/60")}
                      style={tint(value)}>
                      <span>{value == null ? "—" : pct(value)}</span>
                      {cell?.rank != null && <span className="text-[10px] text-muted-foreground">#{cell.rank}</span>}
                    </button>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const helper = createColumnHelper<RotationRow>();

function Count({ count }: { count: { count: number; n: number } }) {
  return count.n ? <span className="tabular-nums">{count.count}/{count.n}</span> : <>—</>;
}

/** The chosen period's rows: every column sorts; the rank is the backend's. */
export function RotationRanking({ data, detail, choice, selected, onSelect }: {
  data: Rotation; detail: RotationDetail; choice: Choice; selected: string | null; onSelect: (id: string) => void;
}) {
  const { t } = useTranslation();
  const interval = data.kind === "interval";
  const columns = useMemo(() => [
    helper.accessor((row) => row.rank ?? undefined, {
      id: "rank", header: () => t("ui.rotation.col.rank"), sortUndefined: "last", sortDescFirst: false,
      cell: ({ getValue }) => <span className="font-semibold tabular-nums">{getValue() ?? "—"}</span>,
      meta: { numeric: true, tip: t(`ui.rotation.col.rank_tip.${choice.rank}`) } satisfies Meta,
    }),
    helper.accessor((row) => rotationName(choice.level, row.id), {
      id: "name", header: () => t(`ui.rotation.col.name.${choice.level}`),
      cell: ({ getValue }) => <span className="font-medium">{getValue()}</span>,
    }),
    helper.accessor("etf", {
      header: () => t("ui.sectors.column.etf"),
      cell: ({ row, getValue }) => (
        <Link to={rotationAnalysisLink(getValue(), data.years, interval ? { start: detail.start_mmdd, end: detail.end_mmdd } : { month: detail.month })}
          onClick={(event) => event.stopPropagation()} className="inline-flex items-center gap-1 font-mono text-xs hover:underline"
          title={t(interval ? "ui.rotation.open_interval" : "ui.rotation.open_monthly", { etf: getValue() })}>
          {getValue()}
          <BarChart3 className="size-3 text-muted-foreground" aria-hidden />
          {row.original.status === "no_prices" && <span className="font-sans text-muted-foreground">{t("ui.rotation.no_prices_short")}</span>}
        </Link>
      ),
    }),
    helper.accessor((row) => numeric(row.stats?.median), {
      id: "median", header: () => t("ui.analysis.col.median"), sortUndefined: "last",
      cell: ({ row }) => <Signed value={row.original.stats?.median} />, meta: { numeric: true } satisfies Meta,
    }),
    helper.accessor((row) => numeric(row.stats?.mean), {
      id: "mean", header: () => t("ui.analysis.col.mean"), sortUndefined: "last",
      cell: ({ row }) => <Signed value={row.original.stats?.mean} />, meta: { numeric: true } satisfies Meta,
    }),
    helper.accessor((row) => (row.stats?.n ? row.stats.up / row.stats.n : undefined), {
      id: "up", header: () => t("ui.analysis.col.up"), sortUndefined: "last",
      cell: ({ row }) => row.original.stats ? <UpShare stats={row.original.stats} /> : "—",
      meta: { numeric: true, tip: t("ui.analysis.col.up_tip") } satisfies Meta,
    }),
    helper.accessor((row) => numeric(row.stats?.median_excess), {
      id: "excess", header: () => t("ui.rotation.col.excess"), sortUndefined: "last",
      cell: ({ row }) => <Signed value={row.original.stats?.median_excess} />,
      meta: { numeric: true, tip: t("ui.rotation.col.excess_tip") } satisfies Meta,
    }),
    helper.accessor((row) => (row.stats?.paired_n ? row.stats.beat / row.stats.paired_n : undefined), {
      id: "beat", header: () => t("ui.rotation.col.beat"), sortUndefined: "last",
      cell: ({ row }) => row.original.stats?.paired_n ? <span className="tabular-nums">{row.original.stats.beat}/{row.original.stats.paired_n}</span> : "—",
      meta: { numeric: true } satisfies Meta,
    }),
    helper.accessor((row) => (row.top.n ? row.top.count / row.top.n : undefined), {
      id: "top", header: () => t("ui.rotation.col.top"), sortUndefined: "last",
      cell: ({ row }) => <Count count={row.original.top} />, meta: { numeric: true, tip: t("ui.rotation.col.top_tip") } satisfies Meta,
    }),
    helper.accessor((row) => (row.bottom.n ? row.bottom.count / row.bottom.n : undefined), {
      id: "bottom", header: () => t("ui.rotation.col.bottom"), sortUndefined: "last",
      cell: ({ row }) => <Count count={row.original.bottom} />, meta: { numeric: true, tip: t("ui.rotation.col.bottom_tip") } satisfies Meta,
    }),
    helper.accessor((row) => numeric(yearValue(currentYear(row) ?? {}, choice.rank)), {
      id: "current", header: () => t("ui.rotation.col.current", { year: data.current_year }), sortUndefined: "last",
      cell: ({ row }) => {
        const entry = currentYear(row.original);
        const value = entry ? yearValue(entry, choice.rank) : null;
        if (value == null) return <span className="text-muted-foreground" title={entry ? t(`ui.analysis.status.${entry.status}`) : undefined}>—</span>;
        return <span title={entry?.status === "in_progress" ? t("ui.analysis.status.in_progress") : undefined}>
          <Signed value={value} />{entry?.status === "in_progress" && <span className="align-super text-[9px]">*</span>}
        </span>;
      },
      meta: { numeric: true, tip: t(`ui.rotation.col.current_tip.${choice.rank}`) } satisfies Meta,
    }),
  ], [t, choice.level, choice.rank, data.years, data.current_year, detail.month, detail.start_mmdd, detail.end_mmdd, interval]);
  return (
    <SortableTable data={detail.rows} columns={columns} initial={[{ id: "rank", desc: false }]}
      rowKey={(row) => row.id} selected={selected} onSelect={onSelect} />
  );
}

/**
 * Rows with fewer complete years than asked: "XLC 8/10", or, when most rows
 * share a shortfall (the local history starts within the first year), that
 * count once for the others.
 */
function fewerYears(rows: RotationRow[]) {
  const counted = rows.filter((row) => row.stats).map((row) => ({ etf: row.etf, n: row.stats!.n, target: row.stats!.target_n }));
  if (!counted.some((row) => row.n < row.target)) return null;
  const tally = new Map<number, number>();
  for (const row of counted) tally.set(row.n, (tally.get(row.n) ?? 0) + 1);
  const common = [...tally.entries()].sort((a, b) => b[1] - a[1])[0][0];
  const target = counted[0].target;
  const listed = counted.filter((row) => row.n !== common && row.n < row.target).map((row) => `${row.etf} ${row.n}/${row.target}`).join(", ");
  if (common === target) return i18n.t("ui.rotation.fewer_years", { items: listed });
  return i18n.t(listed ? "ui.rotation.fewer_years_others" : "ui.rotation.fewer_years_all", { items: listed, n: common, target });
}

function numeric(value: string | null | undefined) {
  return value == null ? undefined : Number(value);
}

function currentYear(row: RotationRow) {
  return row.years.find((entry) => entry.current);
}

/** Each ETF's distribution of the chosen period over the complete past years, by rank. */
export function RotationBoxes({ detail, choice, selected, onSelect }: {
  detail: RotationDetail; choice: Choice; selected: string | null; onSelect: (id: string) => void;
}) {
  const { t } = useTranslation();
  const groups = useMemo<BoxGroup[]>(() => detail.rows
    .filter((row) => row.stats && row.rank != null)
    .sort((a, b) => (a.rank ?? 0) - (b.rank ?? 0))
    .map((row) => {
      const stats = row.stats!;
      const box = choice.rank === "relative" ? stats.benchmark_excess_box
        : { ...EMPTY_DISTRIBUTION, n: stats.n, q25: stats.q25, median: stats.median, mean: stats.mean, q75: stats.q75,
          whisker_low: stats.whisker_low, whisker_high: stats.whisker_high, outliers: stats.outliers };
      return {
        key: row.id, label: row.etf, stats: box,
        points: row.years.filter((entry) => entry.status === "complete" || entry.status === "in_progress").map((entry) => ({
          id: row.id, label: `${row.etf} ${entry.year}`, value: yearValue(entry, choice.rank), current: entry.current,
        })),
      };
    }), [detail.rows, choice.rank]);
  if (!groups.length) return null;
  return (
    <div className="rounded-lg border bg-card p-2">
      <BoxPlot groups={groups} title={t("ui.rotation.box_title", { period: rotationPeriodName(detail) })} height={300}
        selected={selected} onSelect={onSelect} onGroup={onSelect} />
    </div>
  );
}

/**
 * A year's first or last: the name on one line, the value under it. Industry
 * group names are too long for a month column, so the group level names the
 * ETF; the full name is in the tooltip.
 */
function Leader({ leader, level, rank, up, etfs }: {
  leader: RotationLeader | null | undefined; level: Level; rank: RankBasis; up: boolean; etfs: Map<string, string>;
}) {
  const { t } = useTranslation();
  if (!leader) return <span className="text-muted-foreground/60">—</span>;
  const value = yearValue(leader, rank);
  const Icon = up ? ArrowUp : ArrowDown;
  const etf = etfs.get(leader.id) ?? "";
  return (
    <span className="flex min-w-0 flex-col" title={`${t(up ? "ui.rotation.first" : "ui.rotation.last")}: ${etf} · ${rotationName(level, leader.id)} ${pct(value, 2)}`}>
      <span className="flex min-w-0 items-center gap-0.5">
        <Icon className="size-2.5 shrink-0 text-muted-foreground" aria-label={t(up ? "ui.rotation.first" : "ui.rotation.last")} />
        <span className={cn("truncate", level === "group" && "font-mono")}>{level === "group" ? etf : rotationName(level, leader.id, true)}</span>
      </span>
      <span className={cn("pl-3 tabular-nums", Number(value) > 0 ? "text-up" : Number(value) < 0 ? "text-down" : "")}>{pct(value)}</span>
    </span>
  );
}

/**
 * Year × month: each cell names that month's first and last, so the actual
 * order of rotation can be read along a row. The current year comes first.
 */
export function RotationYearly({ data, choice, selected, onSelect }: {
  data: Rotation; choice: Choice; selected: number | null; onSelect?: (month: number) => void;
}) {
  const { t } = useTranslation();
  const years = [...(data.periods[0]?.yearly ?? [])].reverse();
  const interval = data.kind === "interval";
  const etfs = useMemo(() => new Map(data.items.map((item) => [item.id, item.etf])), [data.items]);
  return (
    <div className="overflow-x-auto rounded-lg border bg-card">
      <table className={cn("table-fixed border-collapse text-[11px]", interval ? "w-full max-w-md" : "w-full min-w-[1100px]")}>
        <colgroup>
          <col className="w-16" />
          {data.periods.map((period) => <col key={period.key} />)}
        </colgroup>
        <thead>
          <tr className="border-b">
            <th className="px-2 py-1.5 text-left font-medium text-muted-foreground">{t("ui.analysis.col.year")}</th>
            {data.periods.map((period) => (
              <th key={period.key} className={cn("px-1 py-1.5 text-center font-medium text-muted-foreground",
                period.month === data.current_month && !interval && "bg-primary/10")}>
                {interval ? rotationPeriodName(period)
                  : <button type="button" onClick={() => onSelect?.(period.month!)} className={cn("hover:text-foreground", selected === period.month && "text-foreground underline")}>{monthName(period.month!, "short")}</button>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {years.map(({ year, current }) => (
            <tr key={year} className={cn("border-b border-border/60 last:border-b-0", current && "bg-muted/40")}>
              <th className="px-2 py-1 text-left align-top font-medium text-muted-foreground">
                {interval && data.cross_year ? `${year}–${String(year + 1).slice(2)}` : year}
                {current && <span className="block text-[9px] font-normal">{t("ui.rotation.not_counted")}</span>}
              </th>
              {data.periods.map((period) => {
                const entry = period.yearly.find((item) => item.year === year);
                return (
                  <td key={period.key} className={cn("px-1 py-1 align-top", selected === period.month && !interval && "outline outline-1 -outline-offset-1 outline-primary/40")}
                    title={entry?.start_date ? `${formatDay(entry.start_date, { year: true })} → ${formatDay(entry.end_date, { year: true })} · ${t("ui.rotation.participants", { count: entry.participants })}` : undefined}>
                    {entry && entry.participants ? (
                      <span className={cn("flex flex-col gap-1", entry.in_progress && "italic")}>
                        <Leader leader={entry.first} level={choice.level} rank={choice.rank} up etfs={etfs} />
                        <Leader leader={entry.last} level={choice.level} rank={choice.rank} up={false} etfs={etfs} />
                      </span>
                    ) : <span className="text-muted-foreground/60">—</span>}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The chosen period in full: summary sentence, ranking table, box plot. */
export function RotationDetailView({ data, choice }: { data: Rotation; choice: Choice }) {
  const { t } = useTranslation();
  const reduce = useReducedMotion();
  const [selected, setSelected] = useState<string | null>(null);
  const detail = data.detail;
  if (!detail) return null;
  const period = rotationPeriodName(detail);
  const fewer = fewerYears(detail.rows);
  return (
    <motion.div key={`${detail.key}-${choice.rank}`} initial={reduce ? { opacity: 0 } : { opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
      transition={{ duration: reduce ? 0.1 : 0.2, ease: "easeOut" }} className="space-y-5">
      <Section title={t("ui.rotation.ranking_title", { period })} hint={t(`ui.rotation.ranking_hint.${choice.rank}`)}>
        <RotationRanking data={data} detail={detail} choice={choice} selected={selected} onSelect={(id) => setSelected((old) => old === id ? null : id)} />
        {fewer && <p className="text-xs text-muted-foreground">{fewer}</p>}
      </Section>
      <Section title={t("ui.rotation.box_title", { period })} hint={t(`ui.rotation.box_hint.${choice.rank}`)}>
        <RotationBoxes detail={detail} choice={choice} selected={selected} onSelect={(id) => setSelected((old) => old === id ? null : id)} />
      </Section>
    </motion.div>
  );
}

export { Section as RotationSection };
