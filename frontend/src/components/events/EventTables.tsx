import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { createColumnHelper, type ColumnDef } from "@tanstack/react-table";
import { benchmarkName, pct } from "@/lib/analysis";
import {
  WINDOWS,
  fiscalLabel,
  sessionLabel,
  windowFormula,
  windowLabel,
  type EventResult,
  type EventRow,
  type EventStats,
  type WindowKey,
} from "@/lib/events";
import { formatDay } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Quartiles, Signed, SortableTable, num } from "@/components/analysis/Tables";
import { EventKline } from "@/components/events/EventCharts";
import { UpShare } from "@/components/analysis/UpShare";

// Column value types differ per column; TanStack needs `any` to hold them in one list.
/* eslint-disable @typescript-eslint/no-explicit-any */

function Up({ stats }: { stats: EventStats | undefined }) {
  return stats ? <UpShare stats={stats} /> : <>—</>;
}

type StatsRow = { ranks?: Record<string, number>; key: string; label: React.ReactNode; tip?: string; stats: EventStats };

/** N, median, mean, quartiles, up x/N with its 95% interval, |median| and the benchmark columns. */
function useStatColumns(benchmark: string | null) {
  const { t } = useTranslation();
  return useMemo(() => {
    const column = createColumnHelper<StatsRow>();
    return [
      column.accessor((row) => row.stats.n, { id: "n", header: "N", meta: { numeric: true }, cell: ({ row }) => row.original.stats.n }),
      column.accessor((row) => num(row.stats.median), { id: "median", header: t("ui.analysis.col.median"), meta: { numeric: true }, cell: ({ row }) => <span className="font-medium"><Signed value={row.original.stats.median} /></span> }),
      column.accessor((row) => num(row.stats.mean), { id: "mean", header: t("ui.analysis.col.mean"), meta: { numeric: true }, cell: ({ row }) => <Signed value={row.original.stats.mean} /> }),
      column.accessor((row) => num(row.stats.q25), {
        id: "quartiles",
        header: t("ui.analysis.col.quartiles"),
        meta: { numeric: true, tip: t("ui.analysis.col.quartiles_tip") },
        cell: ({ row }) => (row.original.stats.n ? <Quartiles low={row.original.stats.q25} high={row.original.stats.q75} /> : "—"),
      }),
      column.accessor((row) => (num(row.stats.up_ratio)), {
        id: "up",
        header: t("ui.events.col.up"),
        meta: { numeric: true, tip: t("ui.events.col.up_tip") },
        cell: ({ row }) => <Up stats={row.original.stats} />,
      }),
      column.accessor((row) => num(row.stats.abs_median), {
        id: "abs",
        header: t("ui.events.col.abs_median"),
        meta: { numeric: true, tip: t("ui.events.col.abs_median_tip") },
        cell: ({ row }) => pct(row.original.stats.abs_median).replace(/^\+/, ""),
      }),
      ...(benchmark
        ? [
            column.accessor((row) => (num(row.stats.beat_ratio)), {
              id: "beat",
              header: t("ui.analysis.col.beat", { benchmark: benchmarkName(benchmark) }),
              meta: { numeric: true },
              cell: ({ row }) => (row.original.stats.paired_n ? `${row.original.stats.beat}/${row.original.stats.paired_n}` : "—"),
            }),
            column.accessor((row) => num(row.stats.median_excess), {
              id: "excess",
              header: t("ui.analysis.col.median_excess"),
              meta: { numeric: true, tip: t("ui.analysis.col.excess_tip") },
              cell: ({ row }) => <Signed value={row.original.stats.median_excess} />,
            }),
          ]
        : []),
    ] as ColumnDef<StatsRow, any>[];
  }, [t, benchmark]);
}

/** The four windows of one ticker, in time order: before, gap, reaction day, after. */
export function WindowTable({ result, benchmark }: { result: EventResult; benchmark: string | null }) {
  const { t } = useTranslation();
  const stats = useStatColumns(benchmark);
  const columns = useMemo(() => {
    const column = createColumnHelper<StatsRow>();
    return [
      column.accessor("key", {
        id: "window",
        header: t("ui.events.col.window"),
        enableSorting: false,
        cell: ({ row }) => <span className="font-medium whitespace-nowrap" title={row.original.tip}>{row.original.label}</span>,
      }),
      ...stats,
    ] as ColumnDef<StatsRow, any>[];
  }, [stats, t]);
  const data = useMemo<StatsRow[]>(
    () => WINDOWS.map((key) => ({ key, label: windowLabel(key, result.n), tip: windowFormula(key), stats: result.summary[key] })),
    [result],
  );
  return <SortableTable data={data} columns={columns} initial={[]} rowKey={(row) => row.key} />;
}

/** Earnings by fiscal quarter: all events and Q1–Q4 for the chosen window. */
export function QuarterTable({ result, benchmark }: { result: EventResult; benchmark: string | null }) {
  const { t } = useTranslation();
  const [window, setWindow] = useState<WindowKey>("reaction");
  const stats = useStatColumns(benchmark);
  const columns = useMemo(() => {
    const column = createColumnHelper<StatsRow>();
    return [
      column.accessor("key", { id: "quarter", header: t("ui.events.col.quarter"), enableSorting: false, cell: ({ row }) => <span className="font-medium">{row.original.label}</span> }),
      ...stats,
    ] as ColumnDef<StatsRow, any>[];
  }, [stats, t]);
  const data = useMemo<StatsRow[]>(
    () => [
      { key: "all", label: t("ui.events.all_quarters"), stats: result.summary[window] },
      ...(result.quarters ?? []).map((quarter) => ({ key: `Q${quarter.fiscal_quarter}`, label: `Q${quarter.fiscal_quarter}`, stats: quarter.summary[window], ranks: quarter.ranks })),
    ],
    [result, window, t],
  );
  return (
    <div className="space-y-2">
      <div className="inline-flex overflow-hidden rounded-md border text-xs" role="group" aria-label={t("ui.events.col.window")}>
        {WINDOWS.map((key) => (
          <button
            key={key}
            type="button"
            aria-pressed={window === key}
            onClick={() => setWindow(key)}
            className={cn("h-7 border-r px-2.5 last:border-r-0 hover:bg-accent", window === key && "bg-primary text-primary-foreground hover:bg-primary")}
          >
            {windowLabel(key, result.n)}
          </button>
        ))}
      </div>
      <SortableTable data={data} columns={columns} initial={[{ id: "median", desc: true }]} rowKey={(row) => row.key} rank={(row, sorting) => row.ranks?.[`${window}_${sorting[0]?.id === "up" ? "up_ratio" : "median"}_${sorting[0]?.desc ? "desc" : "asc"}`]} />
    </div>
  );
}

/** Several tickers ranked by their reaction-day median; click one to show it. */
export function TickerRankTable({ results, benchmark, selected, onSelect }: { results: EventResult[]; benchmark: string | null; selected: string; onSelect: (symbol: string) => void }) {
  const { t } = useTranslation();
  const columns = useMemo(() => {
    const column = createColumnHelper<EventResult>();
    const median = (key: WindowKey, header: string) =>
      column.accessor((row) => num(row.summary[key]?.median), { id: key, header, meta: { numeric: true }, cell: ({ row }) => <Signed value={row.original.summary[key]?.median} /> });
    return [
      column.accessor("symbol", { id: "ticker", header: t("ui.analysis.col.ticker"), cell: ({ row }) => <span className="font-semibold tabular-nums">{row.original.symbol}</span> }),
      column.accessor((row) => row.summary.reaction?.n ?? 0, { id: "n", header: "N", meta: { numeric: true }, cell: ({ row }) => row.original.summary.reaction?.n ?? 0 }),
      column.accessor((row) => num(row.summary.reaction?.median), {
        id: "reaction",
        header: t("ui.events.col.reaction_median"),
        meta: { numeric: true },
        cell: ({ row }) => <span className="font-medium"><Signed value={row.original.summary.reaction?.median} /></span>,
      }),
      column.accessor((row) => num(row.summary.reaction?.abs_median), {
        id: "abs",
        header: t("ui.events.col.abs_median"),
        meta: { numeric: true, tip: t("ui.events.col.abs_median_tip") },
        cell: ({ row }) => pct(row.original.summary.reaction?.abs_median).replace(/^\+/, ""),
      }),
      column.accessor((row) => (num(row.summary.reaction?.up_ratio)), {
        id: "up",
        header: t("ui.events.col.up"),
        meta: { numeric: true, tip: t("ui.events.col.up_tip") },
        cell: ({ row }) => <Up stats={row.original.summary.reaction} />,
      }),
      median("gap", t("ui.events.col.gap_median")),
      median("before", t("ui.events.col.before_median", { n: results[0]?.n ?? 0 })),
      median("after", t("ui.events.col.after_median", { n: results[0]?.n ?? 0 })),
      ...(benchmark
        ? [column.accessor((row) => (num(row.summary.reaction?.beat_ratio)), {
            id: "beat",
            header: t("ui.analysis.col.beat", { benchmark: benchmarkName(benchmark) }),
            meta: { numeric: true },
            cell: ({ row }) => (row.original.summary.reaction?.paired_n ? `${row.original.summary.reaction.beat}/${row.original.summary.reaction.paired_n}` : "—"),
          })]
        : []),
    ] as ColumnDef<EventResult, any>[];
  }, [t, benchmark, results]);
  return <SortableTable data={results} columns={columns} initial={[{ id: "reaction", desc: true }]} rowKey={(row) => row.symbol} selected={selected} onSelect={onSelect} />;
}

function WindowCell({ row, window, benchmark }: { row: EventRow; window: WindowKey; benchmark: string | null }) {
  const { t } = useTranslation();
  const value = row.windows[window];
  if (!value) return <>—</>;
  if (value.status === "pending") return <span className="text-xs text-muted-foreground">{t("ui.events.pending", { day: formatDay(value.end_date, { year: false }) })}</span>;
  if (value.status === "missing_price") return <span className="text-xs text-muted-foreground">{t("ui.events.missing_price")}</span>;
  return (
    <span className="inline-flex flex-col items-end leading-tight" title={`${formatDay(value.start_date, { year: true })} → ${formatDay(value.end_date, { year: true })}`}>
      <span className={cn(window === "reaction" && "font-medium")}><Signed value={value.value} digits={2} /></span>
      {benchmark && value.benchmark != null && (
        <span className="text-[11px] text-muted-foreground">
          {benchmarkName(benchmark)} {pct(value.benchmark, 2)}
        </span>
      )}
    </span>
  );
}

/** Every event: dates, session, R and the windows (benchmark under each); click a row for its K-line. */
export function EventDetailTable({ result, benchmark, selected, onSelect }: { result: EventResult; benchmark: string | null; selected: string | null; onSelect: (id: string) => void }) {
  const { t } = useTranslation();
  const columns = useMemo(() => {
    const column = createColumnHelper<EventRow>();
    return [
      column.accessor("name", {
        id: "name",
        header: t("ui.events.col.event"),
        cell: ({ row }) => (
          <span className="flex min-w-0 flex-col">
            <span className="font-medium">{row.original.name}</span>
            <span className="text-xs text-muted-foreground">
              {[fiscalLabel(row.original), ...row.original.notes.map((note) => t(`ui.events.note.${note}`)), row.original.note].filter(Boolean).join(" · ")}
            </span>
          </span>
        ),
      }),
      column.accessor("date", {
        id: "date",
        header: t("ui.events.col.date"),
        cell: ({ row }) => (
          <span className="whitespace-nowrap tabular-nums">
            {formatDay(row.original.date, { year: true })}
            <span className="ml-1 text-xs text-muted-foreground">{sessionLabel(row.original.session)}</span>
          </span>
        ),
      }),
      column.accessor("reaction_date", { id: "r", header: t("ui.events.window.reaction"), cell: ({ row }) => <span className="whitespace-nowrap tabular-nums">{formatDay(row.original.reaction_date, { year: true })}</span> }),
      ...WINDOWS.map((key) =>
        column.accessor((row) => num(row.windows[key]?.value), {
          id: key,
          header: windowLabel(key, result.n),
          meta: { numeric: true, tip: windowFormula(key) },
          cell: ({ row }) => <WindowCell row={row.original} window={key} benchmark={benchmark} />,
        }),
      ),
    ] as ColumnDef<EventRow, any>[];
  }, [t, benchmark, result.n]);
  const key = (row: EventRow) => row.id;
  return (
    <SortableTable
      data={result.rows}
      columns={columns}
      initial={[{ id: "reaction", desc: true }]}
      rowKey={key}
      selected={selected}
      onSelect={onSelect}
      expanded={(row) => <div><EventKline row={row} /><p className="text-xs text-muted-foreground">{t("ui.events.kline_legend", { n: result.n })}</p></div>}
      rank={(row, sorting) => row.ranks[`${WINDOWS.includes(sorting[0]?.id as WindowKey) ? sorting[0].id : "reaction"}_${sorting[0]?.desc ? "desc" : "asc"}`]}
      rowTone={(row, sorting) => {
        const window = WINDOWS.includes(sorting[0]?.id as WindowKey) ? sorting[0].id : "reaction";
        return row.ranks[`${window}_desc`] <= 3 ? "bg-up-soft" : row.ranks[`${window}_asc`] <= 3 ? "bg-down-soft" : "";
      }}
    />
  );
}
