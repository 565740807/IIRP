import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { Schemas } from "@/lib/api-client";
import { formatDayRange, formatMoney } from "@/lib/format";
import { readableCase } from "@/lib/insider";
import { sourceContext } from "@/lib/navigation";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
type Trader = Schemas["NewsTrader"];
/** Name, side, amount, trade count and days of one line; shared by every line of the list. */
const LINE = "grid grid-cols-[minmax(0,1fr)_2.75rem_5.25rem_3.25rem_7.5rem] items-baseline gap-x-2";

/**
 * One insider's new trades on one side: the counted amount, how many trades (when more than
 * one) and their trade day or days; opens the newest of them.
 */
function TraderLine({ line }: { line: Trader }) {
  const { t } = useTranslation();
  const location = useLocation();
  const names = line.owners.map((owner) => readableCase(owner.name)).join(t("format.item_separator"));
  const unknown = line.amount == null;
  const note = unknown ? t(line.price_mismatch ? "ui.watchlist.news.amount_suspect" : "ui.watchlist.news.amount_unknown")
    : line.amount_partial || line.price_mismatch ? t("ui.watchlist.news.amount_partial") : null;
  const amount = unknown ? "—" : `${line.amount_estimated ? `${t("ui.overview.estimated")} ` : ""}${formatMoney(line.amount)}${note ? "*" : ""}`;
  return (
    <Link to={`/transactions/${line.id}`} state={sourceContext(location)} data-news-line
      className={cn(LINE, "rounded px-1.5 py-1 text-xs hover:bg-accent/60")}>
      <span className="truncate" title={names}>{names}</span>
      <span className={line.side === "buy" ? "text-up" : "text-down"}>{t(`ui.watchlist.news.side.${line.side}`)}</span>
      {note ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <span className={cn("cursor-help text-right tabular-nums", unknown && "text-muted-foreground")}>{amount}</span>
          </TooltipTrigger>
          <TooltipContent side="top">{note}</TooltipContent>
        </Tooltip>
      ) : <span className="text-right tabular-nums">{amount}</span>}
      <span className="text-right text-muted-foreground tabular-nums">{line.trades > 1 ? t("ui.watchlist.news.trades", { count: line.trades }) : ""}</span>
      <span className="text-right text-muted-foreground tabular-nums">{formatDayRange([line.first_date, line.last_date])}</span>
    </Link>
  );
}


export function InsiderNewsDetail({ row }: { row: Schemas["InsiderNews"] }) {
  const { t } = useTranslation();
  return <div className="space-y-0.5">
    {row.traders.map((line) => <TraderLine key={`${line.id}:${line.side}`} line={line}/>)}
    {row.trader_count > row.traders.length && (
      <Link to={`/companies/${row.issuer_id}`} className="block px-1.5 py-0.5 text-xs text-muted-foreground hover:text-foreground hover:underline">
      {t("ui.watchlist.news.more_people", { count: row.trader_count - row.traders.length })}
      </Link>
    )}
  </div>;
}
