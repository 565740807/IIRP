import { useQuery } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowRight } from "lucide-react";
import { client, unwrap } from "@/lib/api-client";
import { formatDay } from "@/lib/format";
import { amountText, NOTABLE_DEPENDENT, personHref, personName, roleLabel } from "@/lib/notable";
import { sourceContext } from "@/lib/navigation";
import { cn } from "@/lib/utils";
import { ReadingMarks } from "./NotableTable";

const DAYS = 90;
const ROWS = 5;

/** The US Eastern day ``days`` before today, as YYYY-MM-DD. */
function daysAgo(days: number) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" })
    .format(new Date(Date.now() - days * 86_400_000));
}

/**
 * On a company page: followed people's trades in this company in the last 90
 * days, at most five lines; more open the timeline filtered to this code.
 * Nothing renders when there are none.
 */
export function CompanyNotable({ issuerId, ticker }: { issuerId: string; ticker: string | null | undefined }) {
  const { t } = useTranslation();
  const location = useLocation();
  const from = daysAgo(DAYS);
  const query = { issuer_id: issuerId, published_from: from, limit: ROWS, ...(ticker ? { ticker } : {}) };
  const result = useQuery({
    queryKey: ["notable-trades", query, NOTABLE_DEPENDENT],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/notable/trades", { params: { query }, signal })),
    staleTime: 60_000,
  });
  const data = result.data;
  if (!data?.items.length) return null;
  const more = `/notable?${new URLSearchParams({ ...(ticker ? { ticker } : {}), from }).toString()}`;
  return (
    <section className="rounded-lg border bg-card px-3 py-2" data-company-notable>
      <header className="flex flex-wrap items-baseline gap-x-2 pb-1">
        <h3 className="card-title">{t("ui.entity.notable.title")}</h3>
        <span className="text-xs text-muted-foreground">{t("ui.entity.notable.meta", { days: DAYS, count: data.total })}</span>
        {ticker && (
          <Link to={more} state={sourceContext(location)} className="ml-auto inline-flex items-center gap-0.5 text-xs text-muted-foreground hover:text-foreground hover:underline">
            {data.total > ROWS ? t("ui.entity.notable.more", { count: data.total }) : t("ui.entity.notable.open")}<ArrowRight className="size-3" />
          </Link>
        )}
      </header>
      <ul className="divide-y text-sm">
        {data.items.map((trade) => {
          const name = personName(trade.person);
          const amount = amountText(trade, t);
          return (
            <li key={trade.id} className="flex min-w-0 flex-wrap items-baseline gap-x-3 py-1" data-company-notable-row>
              <span className="w-24 shrink-0 text-xs text-muted-foreground tabular-nums">{formatDay(trade.published, { year: true })}</span>
              <Link to={personHref(trade.person.key)} state={sourceContext(location)} className="max-w-56 truncate font-medium hover:underline" title={name}>{name}</Link>
              <span className="max-w-48 truncate text-xs text-muted-foreground" title={roleLabel(trade.person, t)}>{roleLabel(trade.person, t)}</span>
              <span className={cn("font-medium", trade.side === "buy" ? "text-up" : trade.side === "sell" ? "text-down" : "text-muted-foreground")}>
                {trade.side === "sell" && trade.partial ? t("ui.notable.side.partial_sale") : t(`ui.notable.side.${trade.side}`)}
              </span>
              {trade.source === "sec" && trade.side === "sell" && trade.plan === true && <span className="text-xs text-muted-foreground">{t("ui.notable.plan.yes")}</span>}
              {amount && <span className="tabular-nums">{amount}</span>}
              {trade.transaction_date && <span className="text-xs text-muted-foreground">{t("ui.entity.notable.traded", { date: formatDay(trade.transaction_date, { year: true }) })}</span>}
              <span className="ml-auto flex items-center gap-1 text-xs text-muted-foreground">
                <ReadingMarks trade={trade} />
                {trade.url ? <a href={trade.url} target="_blank" rel="noreferrer" className="hover:text-foreground hover:underline">{t(`ui.notable.source.${trade.source}`)}</a>
                  : t(`ui.notable.source.${trade.source}`)}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
