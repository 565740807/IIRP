import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { formatDay, formatMoney } from "@/lib/format";
import { useInsiderActivity, type InsiderActivity } from "@/lib/insiderActivity";
import { readableCase, rolesText } from "@/lib/insider";
import { sourceContext } from "@/lib/navigation";
import { tickerKey, type TickerMarkProps } from "@/lib/tickerMarks";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/** Who traded, how much and when; facts only, as on the Insider pages. */
function ActivityDetail({ item, days }: { item: InsiderActivity; days: number }) {
  const { t } = useTranslation();
  const side = (key: "buy" | "sell") => item[key].trades > 0 && t(`ui.activity.${key}_total`, {
    amount: formatMoney(item[key].amount),
    people: t("ui.activity.people", { count: item[key].people }),
    trades: t("ui.activity.trades", { count: item[key].trades }),
  });
  const totals = [side("buy"), side("sell")].filter(Boolean).join(" · ");
  const more = item.trader_count - item.traders.length;
  return (
    <div className="flex flex-col items-start gap-1.5 py-0.5">
      <p className="font-medium">{t("ui.activity.title", { count: days })}</p>
      <ul className="space-y-1">
        {item.traders.map((trader) => (
          <li key={`${trader.id}:${trader.side}`}>
            <span className="font-medium">{readableCase(trader.name)}</span>
            {trader.roles.length > 0 && <span className="opacity-75"> · {rolesText(trader)}</span>}
            <br/>
            <span className="tabular-nums">
              {t(`ui.activity.trader_${trader.side}`, { amount: formatMoney(trader.amount), trades: t("ui.activity.trades", { count: trader.trades }) })}
              {" · "}{formatDay(trader.last_date)}
            </span>
          </li>
        ))}
      </ul>
      {more > 0 && <p className="opacity-75">{t("ui.activity.more_people", { count: more })}</p>}
      <p className="border-t border-background/20 pt-1.5 tabular-nums">
        {totals}
        {item.buy.trades > 0 && item.sell.trades > 0 && <> · {t("ui.activity.net", { amount: formatMoney(item.net_amount, { signed: true }) })}</>}
      </p>
      <p className="opacity-75">
        {t("ui.activity.latest", { day: formatDay(item.last_date) })}
        {item.plan_trades > 0 && <> · {t("ui.activity.plans", { count: item.plan_trades })}</>}
      </p>
      {item.missing_price_rows > 0 && <p className="opacity-75">{t("ui.overview.partial_amount")}</p>}
      {item.price_mismatch_rows > 0 && <p className="opacity-75">{t("ui.overview.mismatch_excluded", { count: item.price_mismatch_rows })}</p>}
    </div>
  );
}

/**
 * "Insider buy 2 · sell 1" beside a code: insiders with open-market purchases
 * and sales over the last `days` days, by the overview's rules. Hover names
 * the largest three; a click opens the company's page (focus view). Nothing
 * is shown for a code without such trades.
 */
export function InsiderActivityMark({ ticker, batch, days = 30, className }: TickerMarkProps & { days?: number; className?: string }) {
  const { t } = useTranslation();
  const location = useLocation();
  const { data } = useInsiderActivity(batch, days);
  const item = data?.items[tickerKey(ticker)];
  if (!item) return null;
  const label = [
    item.buy.people > 0 && t("ui.activity.buy_people", { count: item.buy.people }),
    item.sell.people > 0 && t("ui.activity.sell_people", { count: item.sell.people }),
  ].filter(Boolean).join(" · ");
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Link
          to={`/companies/${item.issuer_id}`}
          state={sourceContext(location)}
          onClick={(event) => event.stopPropagation()}
          aria-label={`${t("ui.activity.mark")} ${label}`}
          data-insider-activity
          className={cn("inline-flex shrink-0 items-baseline gap-1 rounded-sm border px-1 text-xs leading-4 whitespace-nowrap tabular-nums hover:bg-accent", className)}
        >
          <span className="text-muted-foreground">{t("ui.activity.mark")}</span>
          {item.buy.people > 0 && <span className="font-medium text-up">{t("ui.activity.buy_people", { count: item.buy.people })}</span>}
          {item.buy.people > 0 && item.sell.people > 0 && <span className="text-muted-foreground" aria-hidden>·</span>}
          {item.sell.people > 0 && <span className="font-medium text-down">{t("ui.activity.sell_people", { count: item.sell.people })}</span>}
        </Link>
      </TooltipTrigger>
      <TooltipContent side="bottom" align="start" className="max-w-sm">
        <ActivityDetail item={item} days={days}/>
      </TooltipContent>
    </Tooltip>
  );
}
