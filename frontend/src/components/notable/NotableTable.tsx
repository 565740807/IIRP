import { Fragment, createContext, useContext, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";
import { ExternalLink, ScanText, TriangleAlert } from "lucide-react";
import { formatDay, formatEt, formatNumber, formatPrice } from "@/lib/format";
import { readableCase } from "@/lib/insider";
import { useVisibleStocks } from "@/lib/insiderOverview";
import { amountText, personHref, personName, roleLabel, tradeHref, windowKeys, type NotableTrade, type NotableReport } from "@/lib/notable";
import { usePageVisible } from "@/lib/refresh";
import { useInsiderN, useTradeWindows, type Windows } from "@/lib/windows";
import { sourceContext } from "@/lib/navigation";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { PageContents } from "@/components/ui/page-contents";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useTableColumns } from "@/components/ui/table-columns";
import { WindowCell } from "@/components/insider/WindowCell";
import { FollowStar } from "./FollowStar";

const column = createColumnHelper<NotableTrade>();
// In the column settings: who held the asset (House/OGE), and shares and price (SEC).
const HIDDEN = ["owner", "shares"];
const EMPTY: NotableTrade[] = [];

function sideClass(side: NotableTrade["side"]) {
  return side === "buy" ? "text-up" : side === "sell" ? "text-down" : "text-muted-foreground";
}

/** The neutral "scanned" mark and the "needs review" mark with its stated doubts. */
export function ReadingMarks({ trade }: { trade: Pick<NotableTrade, "ocr" | "review"> }) {
  const { t } = useTranslation();
  return <>
    {trade.ocr && (
      <Tooltip>
        <TooltipTrigger asChild>
          <span data-mark="scanned" tabIndex={0} className="inline-flex shrink-0 cursor-default items-center gap-0.5 rounded border px-1 text-xs text-muted-foreground">
            <ScanText className="size-3" aria-hidden />{t("ui.notable.scanned")}
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs">{t("ui.notable.scanned_tip")}</TooltipContent>
      </Tooltip>
    )}
    {trade.review.length > 0 && (
      <Tooltip>
        <TooltipTrigger asChild>
          <span data-mark="review" tabIndex={0} className="inline-flex shrink-0 cursor-default items-center gap-0.5 rounded border border-warn/40 px-1 text-xs text-warn">
            <TriangleAlert className="size-3" aria-hidden />{t("ui.notable.review")}
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs flex-col items-start gap-0.5 text-left">
          <span className="font-medium">{t("ui.notable.review_tip")}</span>
          {trade.review.map((reason) => <span key={reason}>· {t(`ui.notable.reason.${reason}`)}</span>)}
        </TooltipContent>
      </Tooltip>
    )}
  </>;
}

/** The asset: the code and name, or the reported asset name; bonds say so. Option and reading marks below. */
function Asset({ trade }: { trade: NotableTrade }) {
  const { t } = useTranslation();
  const location = useLocation();
  // SEC company names often arrive in capitals; disclosure assets stay as reported.
  const name = trade.source === "sec" ? readableCase(trade.asset_name ?? "") : trade.asset_name ?? "";
  const body = (
    <span className="flex min-w-0 items-baseline gap-1.5">
      {trade.ticker ? <span className="shrink-0 rounded bg-secondary px-1 text-xs font-semibold tabular-nums">{trade.ticker}</span>
        : trade.asset_kind === "bond" ? <span className="shrink-0 rounded border px-1 text-xs text-muted-foreground">{t("ui.notable.bond")}</span>
          : null}
      <span className="min-w-0 truncate">{name || "—"}</span>
    </span>
  );
  const marks = trade.asset_kind === "option" || trade.ocr || trade.review.length > 0;
  return (
    <div className="w-56 min-w-0" title={name}>
      {trade.issuer_id && trade.asset_kind !== "bond" && trade.asset_kind !== "fund"
        ? <Link to={`/companies/${trade.issuer_id}`} state={sourceContext(location)} className="block hover:underline">{body}</Link>
        : body}
      {marks && (
        <span className="mt-0.5 flex flex-wrap items-center gap-1">
          {trade.asset_kind === "option" && <span className="text-xs text-muted-foreground">{t("ui.notable.option")}</span>}
          <ReadingMarks trade={trade} />
        </span>
      )}
    </div>
  );
}

/** Buy or sell (a Form 4 sale says whether it was under a plan) over the amount: exact for Form 4, a range for House and OGE. */
function SideAmount({ trade }: { trade: NotableTrade }) {
  const { t } = useTranslation();
  const label = trade.side === "sell" && trade.partial ? t("ui.notable.side.partial_sale") : t(`ui.notable.side.${trade.side}`);
  const amount = amountText(trade, t);
  return (
    <div className="leading-tight whitespace-nowrap">
      <span className={cn("font-medium", sideClass(trade.side))}>{label}</span>
      {trade.source === "sec" && trade.side === "sell" && (
        <span className="ml-1 text-xs text-muted-foreground" title={t("ui.notable.plan_tip")} data-plan={String(trade.plan)}>
          {t(trade.plan === true ? "ui.notable.plan.yes" : trade.plan === false ? "ui.notable.plan.no" : "ui.notable.plan.unknown")}
        </span>
      )}
      {trade.amendment && <Badge variant="outline" className="ml-1 h-4 px-1 text-xs" title={t("ui.notable.amended_tip")}>{t("ui.notable.amended")}</Badge>}
      <span className="block text-xs tabular-nums" title={trade.amount == null ? t("ui.notable.range_tip") : t("ui.notable.exact_tip")}>
        {amount ?? <span className="text-muted-foreground">—</span>}
      </span>
    </div>
  );
}

/** The publication day (opens the trade) over the source, linked to the original filing or report. */
function Published({ trade }: { trade: NotableTrade }) {
  const { t } = useTranslation();
  const location = useLocation();
  const [params] = useSearchParams();
  const n = params.get("n");
  const label = t(`ui.notable.source.${trade.source}`);
  return (
    <div className="leading-tight whitespace-nowrap">
      <Link to={`${tradeHref(trade)}${n ? `?n=${n}` : ""}`} state={sourceContext(location)} className="font-medium tabular-nums hover:underline"
        title={trade.published_at ? t("ui.notable.accepted_at", { time: formatEt(trade.published_at) }) : t("ui.notable.open_trade")}>
        {formatDay(trade.published, { year: true })}
      </Link>
      {trade.url ? (
        <a href={trade.url} target="_blank" rel="noreferrer" className="flex w-fit items-center gap-0.5 text-xs text-muted-foreground hover:text-foreground hover:underline"
          title={t("ui.notable.open_original")} data-source-link>
          {label}<ExternalLink className="size-3" aria-hidden />
        </a>
      ) : <span className="block text-xs text-muted-foreground">{label}</span>}
    </div>
  );
}

/**
 * The windows and n reach the cells through context, so the column definitions stay the
 * same while prices load and cells (with an open tooltip) are not remounted.
 */
const WindowsContext = createContext<{ windows: Windows; n: number } | null>(null);

function WindowHead({ which }: { which: "before" | "after" | "published" }) {
  const { t } = useTranslation();
  const n = useContext(WindowsContext)?.n ?? 0;
  return <span title={t(`ui.notable.window_head_long.${which}`, { n })}>{t(`ui.notable.window_head.${which}`, { n })}</span>;
}

/** One window cell, or a quiet dash with the reason for rows that have no stock code. */
function Window({ trade, which }: { trade: NotableTrade; which: "before" | "after" | "published" }) {
  const { t } = useTranslation();
  const { windows, n } = useContext(WindowsContext)!;
  const day = which === "published" ? trade.published_basis : trade.transaction_date;
  if (!trade.price_ticker || !day) {
    const reason = !trade.price_ticker ? t(`ui.notable.no_window.${trade.price_none ?? "no_ticker"}`) : t("ui.notable.no_window.no_date");
    return <span className="cursor-default text-muted-foreground" title={reason} aria-label={reason}>—</span>;
  }
  const item = windows.get({ ticker: trade.price_ticker, date: day });
  return (
    <WindowCell n={n} side={which === "before" ? "before" : "after"} basis={which === "published" ? "published" : "trade"}
      ticker={windows.ticker(trade.price_ticker)} change={which === "before" ? item?.before : item?.after} />
  );
}

/**
 * Followed people's trades from the three sources, one row each: published (with
 * the original), person, asset, buy or sell with the amount (exact for Form 4, a
 * range for House and OGE), the trade date with how long it took to publish, and
 * the change n sessions before and after the trade and after publication. Window
 * prices are asked for the rows on screen only. A row opens the trade.
 * ``person`` false hides the person column (one person's page).
 */
export function NotableTable({ rows: flatRows = EMPTY, groups, person = true, toolbar, footer, empty, table: tableId }: {
  rows?: NotableTrade[]; groups?: NotableReport[]; person?: boolean; toolbar?: ReactNode; footer?: ReactNode; empty?: ReactNode; table: string;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const rows = useMemo(() => groups ? groups.flatMap((group) => expanded.has(group.id) ? group.trades
    : group.preview_ids.flatMap((id) => group.trades.filter((trade) => trade.id === id))) : flatRows, [groups, expanded, flatRows]);
  const location = useLocation();
  const navigate = useNavigate();
  const { n } = useInsiderN();
  const visible = usePageVisible();
  const body = useRef<HTMLTableSectionElement>(null);
  const shown = useVisibleStocks(body, rows.map((row) => row.id).join(","));
  const keys = useMemo(() => windowKeys(rows, shown.ids), [rows, shown.ids]);
  const windows = useTradeWindows(keys, n, visible, true);
  const columns = useMemo(() => [
    ...(!groups ? [column.accessor((row) => row.published ?? "", {
      id: "published",
      enableHiding: false,
      header: t("ui.notable.column.published"),
      cell: ({ row }) => <Published trade={row.original} />,
    })] : []),
    ...(person && !groups ? [column.accessor((row) => row.person.name, {
      id: "person",
      enableHiding: false,
      header: t("ui.notable.column.person"),
      cell: ({ row }) => {
        const trade = row.original;
        const name = personName(trade.person);
        const role = roleLabel(trade.person, t);
        return (
          <div className="flex w-48 min-w-0 items-start gap-0.5">
            <FollowStar personKey={trade.person.key} name={name} size="xs" className="-ml-1" />
            <div className="min-w-0 leading-tight">
              <Link to={personHref(trade.person.key)} state={sourceContext(location)} className="block truncate font-medium hover:underline" title={name}>{name}</Link>
              <span className="block truncate text-xs text-muted-foreground" title={[role, trade.person.title].filter(Boolean).join(" — ")}>
                {role}
                {trade.others.length > 0 && <span title={trade.others.map((other) => personName(other)).join(", ")}> · {t("ui.notable.others", { count: trade.others.length })}</span>}
              </span>
            </div>
          </div>
        );
      },
    })] : []),
    column.accessor((row) => row.ticker ?? row.asset_name ?? "", {
      id: "asset",
      enableHiding: false,
      header: t("ui.notable.column.asset"),
      cell: ({ row }) => <Asset trade={row.original} />,
    }),
    column.accessor("side", {
      id: "side",
      header: t("ui.notable.column.side_amount"),
      cell: ({ row }) => <SideAmount trade={row.original} />,
    }),
    column.accessor((row) => row.shares ?? "", {
      id: "shares",
      header: t("ui.notable.column.shares"),
      meta: { numeric: true },
      cell: ({ row }) => row.original.shares == null ? <span className="text-muted-foreground">—</span>
        : `${formatNumber(row.original.shares, 0)} @ ${formatPrice(row.original.price, row.original.currency && row.original.currency !== "USD" ? row.original.currency : undefined)}`,
    }),
    column.accessor((row) => row.transaction_date ?? "", {
      id: "traded",
      header: t("ui.notable.column.traded"),
      cell: ({ row }) => {
        const trade = row.original;
        return (
          <div className="leading-tight whitespace-nowrap tabular-nums">
            {trade.transaction_date ? formatDay(trade.transaction_date, { year: true }) : <span className="text-muted-foreground">—</span>}
            {trade.lag_days != null && trade.lag_days >= 0 && (
              <span className="block text-xs text-muted-foreground">{t("ui.notable.lag", { count: trade.lag_days })}</span>
            )}
          </div>
        );
      },
    }),
    ...(["before", "after", "published"] as const).map((which) => column.display({
      id: `window_${which}`,
      header: () => <WindowHead which={which} />,
      meta: { numeric: true },
      cell: ({ row }) => <Window trade={row.original} which={which} />,
    })),
    column.accessor((row) => row.owner ?? "", {
      id: "owner",
      header: t("ui.notable.column.owner"),
      cell: ({ row }) => row.original.owner ? t(`ui.notable.owner.${row.original.owner}`, { defaultValue: row.original.owner }) : <span className="text-muted-foreground">—</span>,
    }),
  ], [t, location, person, !!groups]);
  const shared = useMemo(() => ({ windows, n }), [windows, n]);
  const choices = useTableColumns(tableId, HIDDEN);
  const table = useReactTable({
    data: rows,
    columns,
    state: { columnVisibility: choices.state(columns.map((item) => item.id!)) },
    getCoreRowModel: getCoreRowModel(),
    getRowId: (row) => row.id,
  });
  // A click anywhere on a row opens the trade, except on its own links and buttons.
  const open = (event: MouseEvent<HTMLTableRowElement>, trade: NotableTrade) => {
    if ((event.target as HTMLElement).closest("a, button, input, [role=button]") || window.getSelection()?.toString()) return;
    navigate(`${tradeHref(trade)}${location.search.includes("n=") ? `?n=${n}` : ""}`, { state: sourceContext(location) });
  };
  return (
    <WindowsContext.Provider value={shared}><div>
      <div className="flex min-h-7 flex-wrap items-center gap-x-3 gap-y-1 pb-1.5">
        {toolbar}
        {choices.settings(table, "ml-auto")}
      </div>
      {groups && groups.length > 1 && <PageContents className="mb-2" items={groups.map((group) => ({
        id: `report-${group.id}`, label: `${formatDay(group.published)} · ${personName(group.person)}`,
      }))} />}
      {!rows.length && empty ? empty : <div className="overflow-x-auto rounded-lg border bg-card">
        <table className="w-full text-sm" data-notable-table>
          <thead>
            {table.getHeaderGroups().map((group) => (
              <tr key={group.id} className="border-b">
                {group.headers.map((header) => {
                  const numeric = (header.column.columnDef.meta as { numeric?: boolean } | undefined)?.numeric;
                  return (
                    <th key={header.id} className={cn("px-2 py-2 text-xs font-medium whitespace-nowrap text-muted-foreground first:pl-3", numeric ? "text-right" : "text-left")}>
                      {flexRender(header.column.columnDef.header, header.getContext())}
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody ref={body}>
            {table.getRowModel().rows.map((row, index, rendered) => {
              const group = groups?.find((item) => item.trades.some((trade) => trade.id === row.original.id));
              const first = group && (index === 0 || !group.trades.some((trade) => trade.id === rendered[index - 1].original.id));
              const last = group && (index === rendered.length - 1 || !group.trades.some((trade) => trade.id === rendered[index + 1].original.id));
              const expandable = group && group.trades.length > group.preview_ids.length;
              return <Fragment key={row.id}>
              {first && <tr id={`report-${group.id}`} data-report-group={group.source} className="scroll-mt-28 border-t bg-secondary/50">
                <td colSpan={table.getVisibleLeafColumns().length} className="px-3 py-2">
                  <ReportHeading group={group} />
                </td>
              </tr>}
              <tr key={row.id} data-trade-row={row.original.source} data-quote-symbol={row.original.price_ticker ?? undefined} data-price-ids={row.original.id}
                onClick={(event) => open(event, row.original)} className="cursor-pointer border-b last:border-b-0 hover:bg-accent/40">
                {row.getVisibleCells().map((cell) => {
                  const numeric = (cell.column.columnDef.meta as { numeric?: boolean } | undefined)?.numeric;
                  return (
                    <td key={cell.id} className={cn("px-2 py-1.5 align-top first:pl-3", numeric && "text-right whitespace-nowrap tabular-nums")}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  );
                })}
              </tr>
              {last && group && expandable && <tr className="border-b">
                <td colSpan={table.getVisibleLeafColumns().length} className="px-3 py-1">
                  <Button size="xs" variant="ghost" data-expand-report={group.id} aria-expanded={expanded.has(group.id)}
                    onClick={() => setExpanded((previous) => {
                      const next = new Set(previous);
                      if (next.has(group.id)) next.delete(group.id); else next.add(group.id);
                      return next;
                    })}>
                    {t(expanded.has(group.id) ? "ui.notable.group.collapse" : "ui.notable.group.expand", { count: group.trades.length })}
                  </Button>
                </td>
              </tr>}
              </Fragment>;
            })}
          </tbody>
        </table>
      </div>}
      {footer}
    </div></WindowsContext.Provider>
  );
}

/** One original report, its publication, people and whole-report counts. */
function ReportHeading({ group }: { group: NotableReport }) {
  const { t } = useTranslation();
  const location = useLocation();
  return <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm" data-report-heading>
    <span className="shrink-0 font-medium tabular-nums" title={group.published_at ? formatEt(group.published_at) : undefined}>
      {formatDay(group.published, { year: true })}
    </span>
    {[group.person, ...group.others].map((person) => <span key={person.key} className="inline-flex items-center gap-1">
      <FollowStar personKey={person.key} name={personName(person)} size="xs" />
      <Link to={personHref(person.key)} state={sourceContext(location)} className="font-medium hover:underline">{personName(person)}</Link>
      <span className="text-xs text-muted-foreground" title={person.title ?? undefined}>{roleLabel(person, t)}</span>
    </span>)}
    {group.url ? <a href={group.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-xs text-muted-foreground hover:underline" data-source-link title={t("ui.notable.open_original")}>
      {t(`ui.notable.source.${group.source}`)}<ExternalLink className="size-3" aria-hidden />
    </a> : <span className="text-xs text-muted-foreground">{t(`ui.notable.source.${group.source}`)}</span>}
    {group.amendment && <Badge variant="outline">{t("ui.notable.amended")}</Badge>}
    <span className="text-xs text-muted-foreground tabular-nums" data-report-counts>
      {t("ui.notable.group.equities", { count: group.equities, buy: group.sides.buy ?? 0, sell: group.sides.sell ?? 0 })}
      {(["bond", "fund", "other"] as const).filter((kind) => group.counts[kind]).map((kind) =>
        <span key={kind}>; {t(`ui.notable.group.${kind}`, { count: group.counts[kind] })}</span>)}
    </span>
  </div>;
}
