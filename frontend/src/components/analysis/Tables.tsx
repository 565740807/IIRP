import { Fragment, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import { ArrowDown, ArrowUp } from "lucide-react";
import { benchmarkName, pct, periodName, yearLabel, type Candle, type Period, type PeriodStats } from "@/lib/analysis";
import { directionClass, formatDay, formatPrice } from "@/lib/format";
import { UpShare } from "@/components/analysis/UpShare";
import { cn } from "@/lib/utils";

// Column value types differ per column; TanStack needs `any` to hold them in one list.
/* eslint-disable @typescript-eslint/no-explicit-any */
export type Meta = { numeric?: boolean; tip?: string };

/**
 * Sortable TanStack table with the app's compact styling. ``expanded`` renders
 * a full-width row under the selected one (the event details' K-line).
 */
export function SortableTable<T>({
  data,
  columns,
  initial,
  selected,
  onSelect,
  rowKey,
  expanded,
  rank,
  rowTone,
  pinnedTop,
}: {
  data: T[];
  columns: ColumnDef<T, any>[];
  initial: SortingState;
  selected?: string | null;
  onSelect?: (key: string) => void;
  rowKey: (row: T) => string;
  expanded?: (row: T) => ReactNode;
  rank?: (row: T, sorting: SortingState) => number | undefined;
  rowTone?: (row: T, sorting: SortingState) => string;
  pinnedTop?: string;
}) {
  const { t } = useTranslation();
  const [sorting, setSorting] = useState<SortingState>(initial);
  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getRowId: rowKey,
    sortDescFirst: true,
  });
  return (
    <div className="overflow-x-auto rounded-lg border bg-card">
      <table className="w-full text-sm">
        <thead>
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id} className="border-b">
              {rank && <th className="px-2 py-2 text-xs text-muted-foreground">{t("ui.analysis.col.rank")}</th>}
              {group.headers.map((header) => {
                const meta = header.column.columnDef.meta as Meta | undefined;
                const sorted = header.column.getIsSorted();
                return (
                  <th key={header.id} title={meta?.tip} className={cn("px-2 py-2 text-xs font-medium whitespace-nowrap text-muted-foreground first:pl-3 last:pr-3", meta?.numeric ? "text-right" : "text-left")}>
                    {header.column.getCanSort() ? (
                      <button type="button" onClick={header.column.getToggleSortingHandler()} className={cn("inline-flex items-center gap-0.5 hover:text-foreground", sorted && "text-foreground")}>
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {sorted === "asc" ? <ArrowUp className="size-3" /> : sorted === "desc" ? <ArrowDown className="size-3" /> : null}
                      </button>
                    ) : (
                      flexRender(header.column.columnDef.header, header.getContext())
                    )}
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {[...table.getRowModel().rows.filter((row) => row.id === pinnedTop),
            ...table.getRowModel().rows.filter((row) => row.id !== pinnedTop)].map((row) => (
            <Fragment key={row.id}>
              <tr
                onClick={onSelect ? () => onSelect(row.id) : undefined}
                aria-selected={selected === row.id}
                aria-expanded={expanded ? selected === row.id : undefined}
                className={cn("border-b last:border-b-0", onSelect && "cursor-pointer hover:bg-accent/50", rowTone?.(row.original, sorting), selected === row.id && "bg-accent hover:bg-accent ring-2 ring-inset ring-primary/50")}
              >
                {rank && <td className="px-2 py-1.5 text-center tabular-nums">{rank(row.original, sorting) ?? "—"}</td>}
                {row.getVisibleCells().map((cell) => {
                  const meta = cell.column.columnDef.meta as Meta | undefined;
                  return (
                    <td key={cell.id} className={cn("px-2 py-1.5 first:pl-3 last:pr-3", meta?.numeric && "text-right whitespace-nowrap tabular-nums")}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  );
                })}
              </tr>
              {expanded && selected === row.id && (
                <tr className="border-b bg-muted/30 last:border-b-0">
                  <td colSpan={row.getVisibleCells().length + (rank ? 1 : 0)} className="px-3 py-2">
                    {expanded(row.original)}
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export const Signed = ({ value, digits = 1 }: { value: unknown; digits?: number }) => <span className={directionClass(value)}>{pct(value, digits)}</span>;

/** The middle half "q25 to q75", never cut and never read as an ellipsis. */
export function Quartiles({ low, high }: { low: unknown; high: unknown }) {
  const { t } = useTranslation();
  return (
    <span className="whitespace-nowrap">
      <Signed value={low} />
      <span className="mx-1 text-xs text-muted-foreground">{t("ui.analysis.range_to")}</span>
      <Signed value={high} />
    </span>
  );
}

export const num = (value: string | null | undefined) => (value == null ? Number.NEGATIVE_INFINITY : Number(value));

type StatsRow = { key: string; label: string; stats: PeriodStats; extra?: string };

/** The statistic columns shared by the month and ticker rankings. */
function useStatColumns(benchmark: string | null) {
  const { t } = useTranslation();
  return useMemo(() => {
    const column = createColumnHelper<StatsRow>();
    const columns = [
      column.accessor((row) => num(row.stats.median), { id: "median", header: t("ui.analysis.col.median"), meta: { numeric: true }, cell: ({ row }) => <Signed value={row.original.stats.median} /> }),
      column.accessor((row) => num(row.stats.mean), { id: "mean", header: t("ui.analysis.col.mean"), meta: { numeric: true }, cell: ({ row }) => <Signed value={row.original.stats.mean} /> }),
      column.accessor((row) => num(row.stats.q25), {
        id: "quartiles",
        header: t("ui.analysis.col.quartiles"),
        meta: { numeric: true, tip: t("ui.analysis.col.quartiles_tip") },
        cell: ({ row }) => (row.original.stats.n ? <Quartiles low={row.original.stats.q25} high={row.original.stats.q75} /> : "—"),
      }),
      column.accessor((row) => (num(row.stats.up_ratio)), {
        id: "up",
        header: t("ui.analysis.col.up"),
        meta: { numeric: true, tip: t("ui.analysis.col.up_tip") },
        cell: ({ row }) => {
          const s = row.original.stats;
          return <UpShare stats={s} />;
        },
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
      column.accessor((row) => num(row.stats.best), {
        id: "best",
        header: t("ui.analysis.col.best"),
        meta: { numeric: true },
        cell: ({ row }) => (
          <span>
            <Signed value={row.original.stats.best} />
            {row.original.stats.best_year != null && <span className="ml-1 text-xs text-muted-foreground">{row.original.stats.best_year}</span>}
          </span>
        ),
      }),
      column.accessor((row) => num(row.stats.worst), {
        id: "worst",
        header: t("ui.analysis.col.worst"),
        meta: { numeric: true },
        cell: ({ row }) => (
          <span>
            <Signed value={row.original.stats.worst} />
            {row.original.stats.worst_year != null && <span className="ml-1 text-xs text-muted-foreground">{row.original.stats.worst_year}</span>}
          </span>
        ),
      }),
    ];
    return columns as ColumnDef<StatsRow, any>[];
  }, [t, benchmark]);
}

/** Months of one ticker, one row each, sorted by median (D11); click a row to choose it. */
export function MonthTable({ periods, benchmark, selected, onSelect }: { periods: Period[]; benchmark: string | null; selected: number; onSelect: (month: number) => void }) {
  const { t } = useTranslation();
  const stats = useStatColumns(benchmark);
  const columns = useMemo(() => {
    const column = createColumnHelper<StatsRow>();
    return [column.accessor((row) => Number(row.key), { id: "month", header: t("ui.analysis.col.month"), cell: ({ row }) => <span className="font-medium">{row.original.label}</span> }), ...stats] as ColumnDef<StatsRow, any>[];
  }, [stats, t]);
  const data = useMemo(() => periods.map((period) => ({ key: period.key, label: periodName(period), stats: period.stats })), [periods]);
  return <SortableTable data={data} columns={columns} initial={[{ id: "median", desc: true }]} rowKey={(row) => row.key} selected={String(selected)} onSelect={(key) => onSelect(Number(key))} />;
}

/** The chosen month or interval for every ticker, ranked by median. */
export function TickerTable({ rows, benchmark, selected, onSelect }: { rows: { symbol: string; period: Period }[]; benchmark: string | null; selected: string; onSelect: (symbol: string) => void }) {
  const { t } = useTranslation();
  const stats = useStatColumns(benchmark);
  const columns = useMemo(() => {
    const column = createColumnHelper<StatsRow>();
    return [
      column.accessor("key", { id: "ticker", header: t("ui.analysis.col.ticker"), cell: ({ row }) => <span className="font-semibold tabular-nums">{row.original.key}</span> }),
      column.accessor((row) => row.stats.n, { id: "n", header: "N", meta: { numeric: true, tip: t("ui.analysis.col.n_tip") }, cell: ({ row }) => `${row.original.stats.n}/${row.original.stats.target_n}` }),
      ...stats,
    ] as ColumnDef<StatsRow, any>[];
  }, [stats, t]);
  const data = useMemo(() => rows.map((row) => ({ key: row.symbol, label: row.symbol, stats: row.period.stats })), [rows]);
  return <SortableTable data={data} columns={columns} initial={[{ id: "median", desc: true }]} rowKey={(row) => row.key} selected={selected} onSelect={onSelect} />;
}

/** Every year of the chosen period: actual first and last sessions, open, close and the benchmark's change. */
export function DetailTable({ period, benchmark }: { period: Period; benchmark: string | null }) {
  const { t } = useTranslation();
  const columns = useMemo(() => {
    const column = createColumnHelper<Candle>();
    const list = [
      column.accessor("year", {
        header: t("ui.analysis.col.year"),
        cell: ({ row }) => (
          <span className="font-medium tabular-nums">
            {yearLabel(row.original, period.cross_year)}
            {row.original.current && <span className="ml-1.5 rounded bg-secondary px-1 text-[11px] font-normal text-muted-foreground">{t(row.original.status === "in_progress" ? "ui.analysis.status.in_progress" : "ui.analysis.this_year")}</span>}
          </span>
        ),
      }),
      column.accessor((row) => row.start_date ?? "", {
        id: "dates",
        header: t("ui.analysis.col.sessions"),
        enableSorting: false,
        cell: ({ row }) =>
          row.original.start_date ? (
            <span className="whitespace-nowrap tabular-nums" title={row.original.missing_dates?.length ? t("ui.analysis.missing_dates", { dates: row.original.missing_dates.join(", ") }) : undefined}>
              {formatDay(row.original.start_date, { year: false })} → {formatDay(row.original.end_date, { year: false })}
              <span className="ml-1 text-xs text-muted-foreground">
                {t("ui.analysis.session_count", { count: row.original.sessions })}
                {row.original.missing_dates?.length ? ` · ${t("ui.analysis.missing_count", { count: row.original.missing_dates.length })}` : ""}
              </span>
            </span>
          ) : (
            <span className="text-muted-foreground">{t(`ui.analysis.status.${row.original.status}`)}</span>
          ),
      }),
      column.accessor((row) => num(row.open), { id: "open", header: t("ui.analysis.col.open"), meta: { numeric: true }, cell: ({ row }) => formatPrice(row.original.open) }),
      column.accessor((row) => num(row.close), { id: "close", header: t("ui.analysis.col.close"), meta: { numeric: true }, cell: ({ row }) => formatPrice(row.original.close) }),
      column.accessor((row) => num(row.change), {
        id: "change",
        header: t("ui.analysis.col.change"),
        meta: { numeric: true },
        cell: ({ row }) =>
          row.original.change == null && row.original.status === "incomplete" ? (
            <span className="text-muted-foreground">{t("ui.analysis.status.incomplete")}</span>
          ) : (
            <span className="font-medium"><Signed value={row.original.change} digits={2} /></span>
          ),
      }),
      ...(benchmark
        ? [
            column.accessor((row) => num(row.benchmark_change), { id: "benchmark", header: benchmarkName(benchmark), meta: { numeric: true }, cell: ({ row }) => <Signed value={row.original.benchmark_change} digits={2} /> }),
            column.accessor((row) => num(row.excess), { id: "excess", header: t("ui.analysis.col.excess"), meta: { numeric: true }, cell: ({ row }) => <Signed value={row.original.excess} digits={2} /> }),
          ]
        : []),
    ];
    return list as ColumnDef<Candle, any>[];
  }, [t, benchmark, period.cross_year]);
  const data = useMemo(() => period.years.filter((candle) => !(candle.current && candle.status === "not_started")).slice().reverse(), [period]);
  return <SortableTable data={data} columns={columns} initial={[{ id: "year", desc: true }]} rowKey={(row) => String(row.year)} />;
}
