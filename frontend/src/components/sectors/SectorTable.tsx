import { useMemo } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { useReducedMotion } from "motion/react";
import * as m from "motion/react-m";
import { ArrowDown, ArrowUp, ChevronRight } from "lucide-react";
import { formatDay, formatSignedRatio } from "@/lib/format";
import { PERIODS, periodChange, type Period, type SectorChange, type SectorItem, type SortColumn } from "@/lib/sectors";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useFlash } from "@/components/home/MarketStrip";
import { ChangeBasis, SinceNote } from "./SectorNotes";
import { INSIDER_DAYS, InsiderHeader, InsiderInfo, InsiderNetCell, insiderNet, type InsiderSectors } from "./InsiderNet";
import { useTableColumns } from "@/components/ui/table-columns";

/** The ETF starts in the column settings: the name cell shows it while it is hidden. */
const HIDDEN_COLUMNS = ["etf"];
type ColumnMeta = { numeric?: boolean; info?: React.ReactNode; label?: React.ReactNode };

const helper = createColumnHelper<SectorItem>();

function changeNumber(change: SectorChange) {
  return change.value == null ? null : Number(change.value);
}

export function ChangeCell({ change, period }: { change: SectorChange; period: Period }) {
  const { t } = useTranslation();
  const value = changeNumber(change);
  const flash = useFlash(period === "today" ? value : null);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className={cn(
          "inline-block cursor-default rounded px-1 tabular-nums transition-colors duration-1000",
          value == null ? "text-muted-foreground" : value > 0 ? "text-up" : value < 0 ? "text-down" : "",
          flash === "up" && "bg-up-flash duration-150",
          flash === "down" && "bg-down-flash duration-150",
        )}>
          {value == null ? "—" : formatSignedRatio(value, 2)}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">
        {change.status === "available" || change.status === "missing"
          ? <ChangeBasis change={change} period={period}/>
          : null}
        {change.status !== "available" && <p>{t(`ui.sectors.status.${change.status}`)}</p>}
      </TooltipContent>
    </Tooltip>
  );
}

/** The comparison date shared by most rows of a period, for its column header. */
export function commonStart(items: readonly { periods?: SectorItem["periods"] | null }[], period: Period) {
  const counts = new Map<string, number>();
  for (const item of items) {
    if (!item.periods) continue;
    const change = periodChange({ periods: item.periods }, period);
    if (change.value != null && change.start_date) counts.set(change.start_date, (counts.get(change.start_date) ?? 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? null;
}

/**
 * One row per sector with every period; sorting by a column header changes
 * the URL. Numbers come from the backend; the table only orders them.
 */
export function SectorTable({ items, historyYears, sort, desc, setSort, selected, toggle, drill, insider, columnsSlot }: {
  items: SectorItem[];
  historyYears: number;
  sort: SortColumn;
  desc: boolean;
  setSort: (column: SortColumn, desc: boolean) => void;
  selected: readonly string[];
  toggle: (etf: string) => void;
  /** Opens the sector's industry groups. */
  drill: (sector: string) => void;
  /** Open-market Insider totals by sector (the last column). */
  insider: InsiderSectors | undefined;
  /** Where the column settings button goes (the page's toolbar). */
  columnsSlot: HTMLElement | null;
}) {
  const { t } = useTranslation();
  const reduce = useReducedMotion();
  const starts = useMemo(() => Object.fromEntries(PERIODS.map((period) => [period, commonStart(items, period)])), [items]);
  const choices = useTableColumns("sectors.performance", HIDDEN_COLUMNS);
  const etfShown = choices.visible("etf");
  const insiderBy = useMemo(() => new Map((insider?.sectors ?? []).map((row) => [row.id, row])), [insider]);
  const columns = useMemo(() => [
    helper.display({
      id: "select",
      enableHiding: false,
      header: () => <span className="sr-only">{t("ui.sectors.select")}</span>,
      cell: ({ row }) => (
        <input
          type="checkbox"
          aria-label={t("ui.sectors.select_one", { name: t(`ui.sectors.name.${row.original.id}`) })}
          checked={selected.includes(row.original.etf)}
          onChange={() => toggle(row.original.etf)}
        />
      ),
    }),
    helper.accessor((item) => t(`ui.sectors.name.${item.id}`), {
      id: "name",
      enableHiding: false,
      header: () => t("ui.sectors.column.name"),
      cell: ({ row, getValue }) => (
        <span className="flex flex-col items-start">
          <button type="button" onClick={() => drill(row.original.id)} className="group inline-flex items-center gap-1 text-left font-medium hover:underline"
            title={t("ui.sectors.drill", { count: row.original.groups })}>
            {getValue()}
            <ChevronRight className="size-3.5 text-muted-foreground group-hover:text-foreground" aria-hidden/>
          </button>
          <span className="text-xs text-muted-foreground">
            {!etfShown && <span className="font-mono">{row.original.etf} · </span>}
            {t("ui.sectors.drill", { count: row.original.groups })}
          </span>
          <SinceNote item={row.original} years={historyYears}/>
        </span>
      ),
    }),
    helper.accessor("etf", { header: () => t("ui.sectors.column.etf"), cell: ({ getValue }) => <span className="font-mono text-xs">{getValue()}</span> }),
    ...PERIODS.map((period) => helper.accessor((item) => changeNumber(periodChange(item, period)), {
      id: period,
      header: () => (
        <span className="flex flex-col items-end leading-tight">
          <span>{t(`ui.sectors.period.${period}`)}</span>
          {starts[period] && <span className="text-xs font-normal text-muted-foreground">
            {t("ui.sectors.versus_close", { day: formatDay(starts[period]) })}
          </span>}
        </span>
      ),
      sortUndefined: "last",
      meta: { numeric: true, label: t(`ui.sectors.period.${period}`) },
      cell: ({ row }) => <ChangeCell change={periodChange(row.original, period)} period={period}/>,
    })),
    helper.accessor((item) => insiderNet(insiderBy.get(item.id)), {
      id: "insider",
      header: () => <InsiderHeader/>,
      sortUndefined: "last",
      meta: { numeric: true, info: <InsiderInfo/>, label: t("ui.sectors.column.insider", { count: INSIDER_DAYS }) },
      cell: ({ row }) => <InsiderNetCell totals={insiderBy.get(row.original.id)} data={insider}/>,
    }),
  ], [t, starts, selected, toggle, historyYears, drill, etfShown, insiderBy, insider]);
  // A new array on every render would re-sort and reset table state in a loop.
  const sorting: SortingState = useMemo(() => [{ id: sort, desc }], [sort, desc]);
  const table = useReactTable({
    data: items,
    columns,
    state: { sorting, columnVisibility: choices.state(["etf", ...PERIODS, "insider"]) },
    onSortingChange: (updater) => {
      const next = typeof updater === "function" ? updater(sorting) : updater;
      const first = next[0];
      if (first) setSort(first.id as SortColumn, first.desc);
      else setSort(sort, !desc);
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    sortDescFirst: true,
    enableSortingRemoval: false,
    autoResetPageIndex: false,
  });
  return (
    <div className="overflow-x-auto rounded-lg border bg-card">
      {columnsSlot && createPortal(choices.settings(table), columnsSlot)}
      <table className="w-full text-sm">
        <thead>
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id} className="border-b">
              {group.headers.map((header) => {
                const { numeric, info } = (header.column.columnDef.meta as ColumnMeta | undefined) ?? {};
                const sorted = header.column.getIsSorted();
                return (
                  <th key={header.id} className={cn("px-2 py-2 text-xs font-medium whitespace-nowrap text-muted-foreground first:pl-3 last:pr-3", numeric ? "text-right" : "text-left")}>
                    {header.column.getCanSort() ? (
                      <button type="button" onClick={header.column.getToggleSortingHandler()} className={cn("inline-flex items-center gap-0.5 hover:text-foreground", sorted && "text-foreground")}>
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {sorted === "asc" ? <ArrowUp className="size-3"/> : sorted === "desc" ? <ArrowDown className="size-3"/> : null}
                      </button>
                    ) : flexRender(header.column.columnDef.header, header.getContext())}
                    {info}
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <m.tr
              key={row.original.id}
              layout={reduce ? false : "position"}
              transition={{ duration: 0.25, ease: "easeOut" }}
              className={cn("border-b last:border-b-0 hover:bg-accent/40", selected.includes(row.original.etf) && "bg-selected")}
            >
              {row.getVisibleCells().map((cell) => {
                const numeric = (cell.column.columnDef.meta as { numeric?: boolean } | undefined)?.numeric;
                return (
                  <td key={cell.id} className={cn("px-2 py-2 first:pl-3 last:pr-3", numeric && "text-right")}>
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                );
              })}
            </m.tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
