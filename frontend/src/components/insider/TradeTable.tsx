import { useMemo, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ChevronRight } from "lucide-react";
import type { Schemas } from "@/lib/api-client";
import { formatDay, formatEt, formatLocal, formatMoney, formatNumber, formatPrice, formatSignedRatio } from "@/lib/format";
import { actionLabel, readableCase, rolesText, side, sideClass } from "@/lib/insider";
import type { Windows } from "@/lib/windows";
import { sourceContext } from "@/lib/navigation";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useTableColumns } from "@/components/ui/column-settings";
import { WindowCell } from "./WindowCell";

export type Trade = Schemas["InsiderTransaction"];
// Shown from the column settings: the filing time, holdings after the trade, ownership and the 10b5-1 flag.
const TRADE_HIDDEN = ["filed", "after", "ownership", "plan"];
const column = createColumnHelper<Trade>();

/** A filing that names no currency is in US dollars; only another currency is shown. */
export function money(value: unknown, currency: string | null | undefined, signed = false, language?: string) {
  return formatMoney(value, { signed, currency: currency && currency !== "USD" ? currency : undefined, language });
}

export function price(value: unknown, currency: string | null | undefined, language?: string) {
  return formatPrice(value, currency && currency !== "USD" ? currency : undefined, language);
}

function signedAmount(row: Trade) {
  const direction = side(row);
  if (row.amount == null) return null;
  return direction === "sell" ? -Number(row.amount) : Number(row.amount);
}

function plan(flag: string | null | undefined) {
  const value = (flag ?? "").trim().toLowerCase();
  return ["1", "true", "yes"].includes(value) ? "yes" : ["0", "false", "no"].includes(value) ? "no" : null;
}

function DateAnomaly({ row }: { row: Trade }) {
  const { t } = useTranslation();
  if (!row.date_anomaly) return null;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="outline" className="h-5 border-warn/40 px-1 text-xs text-warn">{t("ui.feed.date_anomaly")}</Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">
        {t("ui.feed.date_anomaly_detail", {
          trade: formatDay(row.date_anomaly.transaction_date, { year: true }),
          accepted: formatDay(row.date_anomaly.accepted_date, { year: true }),
        })}
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * A company's or person's transactions. Facts only: the action in SEC's words
 * with its code, shares, price, amount, holdings after and their change,
 * ownership form, 10b5-1 plan flag, and the price before/after n sessions.
 */
export function TradeTable({
  rows,
  kind,
  personId,
  windows,
  n,
  selected,
  onSelect,
  toolbar,
  empty,
}: {
  rows: Trade[];
  kind: "company" | "person";
  personId?: string;
  windows: Windows;
  n: number;
  selected: string | null;
  onSelect: (id: string) => void;
  toolbar?: ReactNode;
  empty?: ReactNode;
}) {
  const { t } = useTranslation();
  const location = useLocation();
  const [sorting, setSorting] = useState<SortingState>([]);
  const columns = useMemo(
    () => [
      column.accessor((row) => row.transaction_date ?? "", {
        id: "traded",
        enableHiding: false,
        header: t("ui.trades.traded"),
        cell: ({ row }) => (
          <span className="inline-flex items-center gap-1 whitespace-nowrap tabular-nums">
            {/* The date opens the trade's detail page; a click elsewhere on the row marks it on the chart. */}
            <Link to={`/transactions/${row.original.id}`} state={sourceContext(location)} onClick={(event) => event.stopPropagation()}
              className="inline-flex items-center gap-0.5 font-medium hover:underline" title={t("ui.trades.open")}>
              {formatDay(row.original.transaction_date)}
              <ChevronRight className="size-3.5 text-muted-foreground" aria-hidden />
            </Link>
            <DateAnomaly row={row.original} />
          </span>
        ),
      }),
      column.accessor((row) => row.accepted_at ?? "", {
        id: "filed",
        header: t("ui.trades.filed"),
        cell: ({ getValue }) => (
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="cursor-default whitespace-nowrap text-muted-foreground tabular-nums">{formatEt(getValue() || null, { zone: false })}</span>
            </TooltipTrigger>
            <TooltipContent>{t("ui.time.local", { time: formatLocal(getValue() || null) })}</TooltipContent>
          </Tooltip>
        ),
      }),
      kind === "company"
        ? column.accessor((row) => row.owners.map((owner) => owner.name).join(", "), {
            id: "insider",
            header: t("ui.trades.insider"),
            enableHiding: false,
            cell: ({ row }) => (
              <div className="w-48 min-w-0">
                {row.original.owners.map((owner) => (
                  <div key={owner.id} className="leading-tight">
                    <Link to={`/people/${owner.id}`} state={sourceContext(location)} onClick={(event) => event.stopPropagation()} className="block truncate font-medium hover:underline" title={readableCase(owner.name)}>
                      {readableCase(owner.name)}
                    </Link>
                    {owner.roles.length > 0 && <span className="block truncate text-xs text-muted-foreground" title={rolesText(owner)}>{rolesText(owner)}</span>}
                  </div>
                ))}
              </div>
            ),
          })
        : column.accessor((row) => row.ticker ?? row.issuer_name ?? "", {
            id: "company",
            header: t("ui.trades.company"),
            enableHiding: false,
            cell: ({ row }) => {
              const owner = row.original.owners.find((item) => item.id === personId) ?? row.original.owners[0];
              const company = readableCase(row.original.issuer_name ?? row.original.issuer_id);
              return (
                // A fixed width and a clipped, single-line name: the cell never runs into the action.
                <div className="w-56 min-w-0 leading-tight">
                  <Link to={`/companies/${row.original.issuer_id}`} state={sourceContext(location)} onClick={(event) => event.stopPropagation()} title={company}
                    className="flex max-w-full min-w-0 items-baseline gap-1.5 hover:underline">
                    <span className="shrink-0 rounded bg-secondary px-1 text-xs font-semibold tabular-nums">{row.original.ticker ?? "—"}</span>
                    <span className="min-w-0 truncate font-medium">{company}</span>
                  </Link>
                  {owner && owner.roles.length > 0 && <div className="truncate text-xs text-muted-foreground" title={rolesText(owner)}>{rolesText(owner)}</div>}
                </div>
              );
            },
          }),
      column.accessor((row) => `${row.table}${row.code}`, {
        id: "action",
        header: t("ui.trades.action"),
        cell: ({ row }) => {
          // Long labels ("Option exercise (M) (derivative)") stay on one line; hover shows all.
          const label = actionLabel(row.original, t);
          return <span title={label} className={cn("block max-w-48 truncate font-medium", sideClass(row.original))}>{label}</span>;
        },
      }),
      column.accessor((row) => Number(row.shares ?? NaN), {
        id: "shares",
        header: t("ui.trades.shares"),
        meta: { numeric: true },
        cell: ({ row }) =>
          row.original.shares == null ? "—" : `${row.original.direction === "D" ? "−" : row.original.direction === "A" ? "+" : ""}${formatNumber(row.original.shares, 0)}`,
      }),
      column.accessor((row) => Number(row.price ?? NaN), {
        id: "price",
        header: t("ui.trades.price"),
        meta: { numeric: true },
        cell: ({ row }) => (row.original.price == null ? <span className="text-muted-foreground">—</span> : price(row.original.price, row.original.currency)),
      }),
      column.accessor((row) => signedAmount(row) ?? NaN, {
        id: "amount",
        header: t("ui.trades.amount"),
        meta: { numeric: true },
        cell: ({ row }) => {
          const value = signedAmount(row.original);
          const direction = side(row.original);
          return value == null ? (
            <span className="text-muted-foreground">—</span>
          ) : (
            <span className={sideClass(row.original)}>{money(value, row.original.currency, direction !== null)}</span>
          );
        },
      }),
      column.accessor((row) => Number(row.shares_after ?? NaN), {
        id: "after",
        header: t("ui.trades.after"),
        meta: { numeric: true },
        cell: ({ row }) => (
          <span className="block leading-tight whitespace-nowrap">
            {row.original.shares_after == null ? "—" : formatNumber(row.original.shares_after, 0)}
            {row.original.shares_before === "0" ? (
              <span className="block text-xs text-muted-foreground">{t("ui.trades.new_position")}</span>
            ) : row.original.holding_change != null ? (
              <span className="block text-xs text-muted-foreground">{formatSignedRatio(row.original.holding_change)}</span>
            ) : null}
          </span>
        ),
      }),
      column.accessor((row) => row.direct_or_indirect ?? "", {
        id: "ownership",
        header: t("ui.trades.ownership"),
        cell: ({ row }) => {
          const value = row.original.direct_or_indirect;
          if (value === "D") return t("ui.trades.direct");
          if (value === "I")
            return (
              <span className="block max-w-28 truncate" title={row.original.nature_of_ownership ?? undefined}>
                {row.original.nature_of_ownership ? t("ui.trades.indirect_by", { nature: row.original.nature_of_ownership }) : t("ui.trades.indirect")}
              </span>
            );
          return <span className="text-muted-foreground">—</span>;
        },
      }),
      column.accessor((row) => plan(row.raw_10b5_1_flag) ?? "", {
        id: "plan",
        meta: { label: t("ui.trades.plan") },
        header: () => (
          <Tooltip>
            <TooltipTrigger className="cursor-default">{t("ui.trades.plan")}</TooltipTrigger>
            <TooltipContent className="max-w-xs">{t("ui.trades.plan_tip")}</TooltipContent>
          </Tooltip>
        ),
        cell: ({ getValue }) => {
          const value = getValue();
          return value ? t(`ui.trades.plan_${value}`) : <span className="text-muted-foreground">—</span>;
        },
      }),
      column.accessor((row) => Number(windows.get({ ticker: row.ticker, date: row.date_anomaly ? null : row.transaction_date })?.before?.value ?? NaN), {
        id: "before",
        meta: { numeric: true, label: t("ui.window.before_head_long", { n }) },
        header: () => <span title={t("ui.window.before_head_long", { n })}>{t("ui.window.before_head", { n })}</span>,
        cell: ({ row }) => (
          <WindowCell n={n} side="before" ticker={windows.ticker(row.original.ticker)}
            change={windows.get({ ticker: row.original.ticker, date: row.original.date_anomaly ? null : row.original.transaction_date })?.before} />
        ),
      }),
      column.accessor((row) => Number(windows.get({ ticker: row.ticker, date: row.date_anomaly ? null : row.transaction_date })?.after?.value ?? NaN), {
        id: "after_n",
        meta: { numeric: true, label: t("ui.window.after_head_long", { n }) },
        header: () => <span title={t("ui.window.after_head_long", { n })}>{t("ui.window.after_head", { n })}</span>,
        cell: ({ row }) => (
          <WindowCell n={n} side="after" ticker={windows.ticker(row.original.ticker)}
            change={windows.get({ ticker: row.original.ticker, date: row.original.date_anomaly ? null : row.original.transaction_date })?.after} />
        ),
      }),
    ],
    [t, kind, personId, location, windows, n],
  );
  const choices = useTableColumns("insider.trades", TRADE_HIDDEN);
  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting, columnVisibility: choices.state(columns.map((item) => item.id!)) },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    sortDescFirst: true,
  });
  return (
    <div>
    <div className="flex min-h-7 flex-wrap items-center gap-x-3 gap-y-1 pb-1.5">
      {toolbar}
      {choices.settings(table, "ml-auto")}
    </div>
    {!rows.length && empty ? empty : (
    <div className="overflow-x-auto rounded-lg border bg-card">
      <table className="w-full text-sm">
        <thead className="bg-card">
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id} className="border-b">
              {group.headers.map((header) => {
                const numeric = (header.column.columnDef.meta as { numeric?: boolean } | undefined)?.numeric;
                const sorted = header.column.getIsSorted();
                return (
                  <th key={header.id} className={cn("px-1.5 py-2 text-xs font-medium whitespace-nowrap text-muted-foreground first:pl-3", numeric ? "text-right" : "text-left")}>
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
          {table.getRowModel().rows.map((row) => (
            <tr
              key={row.id}
              id={`trade-${row.original.id}`}
              onClick={() => onSelect(row.original.id)}
              aria-selected={selected === row.original.id}
              className={cn(
                "cursor-pointer border-b transition-colors duration-500 last:border-b-0 hover:bg-accent/50",
                selected === row.original.id && "bg-selected hover:bg-selected",
              )}
            >
              {row.getVisibleCells().map((cell) => {
                const numeric = (cell.column.columnDef.meta as { numeric?: boolean } | undefined)?.numeric;
                return (
                  <td key={cell.id} className={cn("px-1.5 py-1.5 align-top first:pl-3", numeric && "text-right tabular-nums whitespace-nowrap")}>
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
    )}
    </div>
  );
}
