import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { Schemas } from "@/lib/api-client";
import type { NewsStock } from "@/lib/watchlistNews";
import { formatEt } from "@/lib/format";
import { readableCase } from "@/lib/insider";
import { sourceContext } from "@/lib/navigation";

export function WatchlistNewsDetails({ at, stocks, insider, names }: {
  at: string | null; stocks: NewsStock[];
  insider: Map<string, Schemas["InsiderNews"]>;
  names: Map<string, Schemas["WatchlistEntry"]>;
}) {
  const { t } = useTranslation();
  const location = useLocation();
  return <>
            <p className="text-xs text-muted-foreground">
              {at ? t("ui.watchlist.news.since", { time: formatEt(at) }) : t("ui.watchlist.news.since_added")}
            </p>
            {stocks.map((stock) => {
              const company = insider.get(stock.ticker) ?? names.get(stock.ticker);
              const name = company?.name ? readableCase(company.name) : null;
              return (
                <div key={stock.ticker} className="space-y-1 border-t pt-2 first-of-type:border-t-0 first-of-type:pt-0">
                  <p className="flex min-w-0 items-baseline gap-2 px-1.5 text-sm">
                    {company?.issuer_id
                      ? <Link to={`/companies/${company.issuer_id}`} state={sourceContext(location)} className="font-semibold hover:underline">{stock.ticker}</Link>
                      : <span className="font-semibold">{stock.ticker}</span>}
                    {name && <span className="truncate text-xs text-muted-foreground" title={name}>{name}</span>}
                  </p>
                  {stock.details.map((part) => <div key={part.key}>{part.node}</div>)}
                </div>
              );
            })}
  </>;
}
