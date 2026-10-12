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
import { distribution, type Basis, type SectorReference, benchmarkName, pct, periodName, yearLabel, type Candle, type Period, type PeriodStats } from "@/lib/analysis";
import { directionClass, formatDay, formatPrice } from "@/lib/format";
import { SectorBadge } from "@/components/analysis/References";
import { UpShare } from "@/components/analysis/UpShare";
import { cn } from "@/lib/utils";
import { useTableColumns } from "@/components/ui/column-settings";

// Column value types differ per column; TanStack needs `any` to hold them in one list.
/* eslint-disable @typescript-eslint/no-explicit-any */
export type Meta = { numeric?: boolean; tip?: string; label?: ReactNode };

/**
 * Sortable TanStack table with the app's compact styling. ``expanded`` renders
 * a full-width row under the selected one (the event details' K-line). With a
 * ``tableId`` the header carries a "Columns" button: ``hidden`` columns start
 * there, and the reader's choice is remembered in this browser.
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
  tableId,
  hidden = NO_COLUMNS,
  toolbar,
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
  tableId?: string;
  hidden?: readonly string[];
  toolbar?: ReactNode;
}) {
  const { t } = useTranslation();
  const [sorting, setSorting] = useState<SortingState>(initial);
  const choices = useTableColumns(tableId ?? "", hidden);
  const ids = columns.map((column) => column.id ?? (column as { accessorKey?: string }).accessorKey ?? "");
  const table = useReactTable({
    data,
    columns,
    state: { sorting, columnVisibility: tableId ? choices.state(ids) : {} },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getRowId: rowKey,
    sortDescFirst: true,
  });
  return (
    <div>
    {(tableId || toolbar) && (
      <div className="flex min-h-7 flex-wrap items-center gap-2 pb-1.5">
        {toolbar}
        {tableId && choices.settings(table, "ml-auto")}
      </div>
    )}
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
    </div>
  );
}

const NO_COLUMNS: readonly string[] = [];

/** At most ``limit`` columns show by default, in ``priority`` order; the rest start in the column settings. */
export function hiddenBeyond(ids: readonly string[], priority: readonly string[], limit = 8) {
  const ranked = [...ids].sort((a, b) => (priority.indexOf(a) + 1 || 99) - (priority.indexOf(b) + 1 || 99));
  return ranked.slice(limit);
}

const RANK_PRIORITY = ["month", "ticker", "median", "n", "up", "excess", "sector_excess", "mean", "quartiles", "best", "worst", "beat", "sector_beat", "sector_return"];

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

type StatsRow = { key: string; label: string; stats: PeriodStats; sector?: SectorReference | null; extra?: string };

/** The statistic columns shared by the month and ticker rankings. */
function useStatColumns(benchmark: string | null, sector: boolean, basis: Basis) {
  const { t } = useTranslation();
  return useMemo(() => {
    const column = createColumnHelper<StatsRow>();
    const columns = [
      column.accessor((row) => num(distribution(row.stats, basis).median), { id: "median", header: t("ui.analysis.col.median"), meta: { numeric: true }, cell: ({ row }) => <Signed value={distribution(row.original.stats, basis).median} /> }),
      column.accessor((row) => num(distribution(row.stats, basis).mean), { id: "mean", header: t("ui.analysis.col.mean"), meta: { numeric: true }, cell: ({ row }) => <Signed value={distribution(row.original.stats, basis).mean} /> }),
      column.accessor((row) => num(distribution(row.stats, basis).q25), {
        id: "quartiles",
        header: t("ui.analysis.col.quartiles"),
        meta: { numeric: true, tip: t("ui.analysis.col.quartiles_tip") },
        cell: ({ row }) => (distribution(row.original.stats, basis).n ? <Quartiles low={distribution(row.original.stats, basis).q25} high={distribution(row.original.stats, basis).q75} /> : "—"),
      }),
      column.accessor((row) => (num(row.stats.up_ratio)), {
        id: "up",
        header: t(basis === "absolute" ? "ui.analysis.col.up" : "ui.analysis.basis.absolute_up"),
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
      ...(sector ? [
        column.accessor((row) => num(row.stats.sector_median), { id: "sector_return", header: t("ui.analysis.sector.col.return"), meta: { numeric: true }, cell: ({ row }) => <Signed value={row.original.stats.sector_median} /> }),
        column.accessor((row) => num(row.stats.sector_beat_ratio), { id: "sector_beat", header: t("ui.analysis.sector.col.beat"), meta: { numeric: true }, cell: ({ row }) => row.original.stats.sector_paired_n ? `${row.original.stats.sector_beat}/${row.original.stats.sector_paired_n}` : "—" }),
        column.accessor((row) => num(row.stats.sector_median_excess), { id: "sector_excess", header: t("ui.analysis.sector.col.median_excess"), meta: { numeric: true }, cell: ({ row }) => <Signed value={row.original.stats.sector_median_excess} /> }),
      ] : []),
      column.accessor((row) => num(distribution(row.stats, basis).best), {
        id: "best",
        header: t("ui.analysis.col.best"),
        meta: { numeric: true },
        cell: ({ row }) => (
          <span>
            <Signed value={distribution(row.original.stats, basis).best} />
            {basis === "absolute" && row.original.stats.best_year != null && <span className="ml-1 text-xs text-muted-foreground">{row.original.stats.best_year}</span>}
          </span>
        ),
      }),
      column.accessor((row) => num(distribution(row.stats, basis).worst), {
        id: "worst",
        header: t("ui.analysis.col.worst"),
        meta: { numeric: true },
        cell: ({ row }) => (
          <span>
            <Signed value={distribution(row.original.stats, basis).worst} />
            {basis === "absolute" && row.original.stats.worst_year != null && <span className="ml-1 text-xs text-muted-foreground">{row.original.stats.worst_year}</span>}
          </span>
        ),
      }),
    ];
    return columns as ColumnDef<StatsRow, any>[];
  }, [t, benchmark, sector, basis]);
}

/** Months of one ticker, one row each, sorted by median; click a row to choose it. */
export function MonthTable({ periods, benchmark, sector = false, basis = "absolute", selected, onSelect }: { periods: Period[]; benchmark: string | null; sector?: boolean; basis?: Basis; selected: number; onSelect: (month: number) => void }) {
  const { t } = useTranslation();
  const stats = useStatColumns(benchmark, sector, basis);
  const columns = useMemo(() => {
    const column = createColumnHelper<StatsRow>();
    return [column.accessor((row) => Number(row.key), { id: "month", enableHiding: false, header: t("ui.analysis.col.month"), cell: ({ row }) => <span className="font-medium">{row.original.label}</span> }), ...stats] as ColumnDef<StatsRow, any>[];
  }, [stats, t]);
  const data = useMemo(() => periods.map((period) => ({ key: period.key, label: periodName(period), stats: period.stats })), [periods]);
  const hidden = hiddenBeyond(columns.map((column) => column.id!), RANK_PRIORITY, 8);
  return <SortableTable data={data} columns={columns} initial={[{ id: "median", desc: true }]} rowKey={(row) => row.key} selected={String(selected)} onSelect={(key) => onSelect(Number(key))} tableId="analysis.months" hidden={hidden} />;
}

/** The chosen month or interval for every ticker, ranked by median. */
export function TickerTable({ rows, benchmark, sector = false, basis = "absolute", selected, onSelect }: { rows: { symbol: string; period: Period; sector?: SectorReference | null }[]; benchmark: string | null; sector?: boolean; basis?: Basis; selected: string; onSelect: (symbol: string) => void }) {
  const { t } = useTranslation();
  const stats = useStatColumns(benchmark, sector, basis);
  const columns = useMemo(() => {
    const column = createColumnHelper<StatsRow>();
    return [
      column.accessor("key", { id: "ticker", enableHiding: false, header: t("ui.analysis.col.ticker"), cell: ({ row }) => <span className="font-semibold tabular-nums">{row.original.key}<SectorBadge reference={row.original.sector} /></span> }),
      column.accessor((row) => distribution(row.stats, basis).n, { id: "n", header: "N", meta: { numeric: true, tip: t("ui.analysis.col.n_tip") }, cell: ({ row }) => `${distribution(row.original.stats, basis).n}/${row.original.stats.target_n}` }),
      ...stats,
    ] as ColumnDef<StatsRow, any>[];
  }, [stats, t, basis]);
  const data = useMemo<StatsRow[]>(() => rows.map((row) => ({ key: row.symbol, label: row.symbol, stats: row.period.stats, sector: row.sector })), [rows]);
  const hidden = hiddenBeyond(columns.map((column) => column.id!), RANK_PRIORITY, 8);
  return <SortableTable data={data} columns={columns} initial={[{ id: "median", desc: true }]} rowKey={(row) => row.key} selected={selected} onSelect={onSelect} tableId="analysis.tickers" hidden={hidden} />;
}

const DETAIL_HIDDEN = ["open", "close"];

/** Every year of the chosen period: actual first and last sessions, open, close and the benchmark's change. */
export function DetailTable({ period, benchmark, sector }: { period: Period; benchmark: string | null; sector?: string | null }) {
  const { t } = useTranslation();
  const columns = useMemo(() => {
    const column = createColumnHelper<Candle>();
    const list: ColumnDef<Candle, any>[] = [
      column.accessor("year", {
        id: "year",
        enableHiding: false,
        header: t("ui.analysis.col.year"),
        cell: ({ row }) => (
          <span className="font-medium tabular-nums">
            {yearLabel(row.original, period.cross_year)}
            {row.original.current && <span className="ml-1.5 rounded bg-secondary px-1 text-xs font-normal text-muted-foreground">{t(row.original.status === "in_progress" ? "ui.analysis.status.in_progress" : "ui.analysis.this_year")}</span>}
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
    if (sector) list.push(
      column.accessor((row) => num(row.sector_change), { id: "sector_return", header: t("ui.analysis.sector.col.change", { sector }), meta: { numeric: true }, cell: ({ row }) => <Signed value={row.original.sector_change} digits={2} /> }),
      column.accessor((row) => num(row.sector_excess), { id: "sector_excess", header: t("ui.analysis.sector.col.excess"), meta: { numeric: true }, cell: ({ row }) => <Signed value={row.original.sector_excess} digits={2} /> }),
    );
    return list as ColumnDef<Candle, any>[];
  }, [t, benchmark, sector, period.cross_year]);
  const data = useMemo(() => period.years.filter((candle) => !(candle.current && candle.status === "not_started")).slice().reverse(), [period]);
  return <SortableTable data={data} columns={columns} initial={[{ id: "year", desc: true }]} rowKey={(row) => String(row.year)} tableId="analysis.years" hidden={DETAIL_HIDDEN} />;
}
