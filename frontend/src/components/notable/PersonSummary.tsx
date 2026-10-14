import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { directionClass, formatSignedRatio } from "@/lib/format";
import { NOTABLE_DEPENDENT } from "@/lib/notable";
import { cn } from "@/lib/utils";
import { InfoTip } from "@/components/ui/info";
import { Skeleton } from "@/components/ui/skeleton";

type Stat = Schemas["SummaryStat"];

/** One side: the median and how many rose (x / N); below the server's threshold the values themselves. */
function Side({ stat, side }: { stat: Stat; side: "buy" | "sell" }) {
  const { t } = useTranslation();
  const label = <span className={cn("font-medium", side === "buy" ? "text-up" : "text-down")}>{t(`ui.notable.side.${side}`)}</span>;
  if (!stat.count)
    return <span className="whitespace-nowrap" data-summary-side={side}>{label} <span className="text-muted-foreground" title={t("ui.notable.summary.none_tip")}>—</span></span>;
  return (
    <span className="whitespace-nowrap tabular-nums" data-summary-side={side}>
      {label}{" "}
      {stat.median != null ? (
        <>{t("ui.notable.summary.median")} <span className={cn("font-medium", directionClass(stat.median))}>{formatSignedRatio(stat.median)}</span></>
      ) : (
        stat.values.map((value, index) => (
          <span key={index} className={cn("font-medium", directionClass(value))}>{index > 0 ? ", " : ""}{formatSignedRatio(value)}</span>
        ))
      )}
      <span className="text-muted-foreground"> · {t("ui.notable.summary.up", { up: stat.up, count: stat.count })}</span>
    </span>
  );
}

/**
 * One line of description, never a signal: how this person's coded stock trades moved
 * n sessions after the trade and after publication, buys and sales apart, from prices
 * already stored. Trades still without prices are counted in the note.
 */
export function PersonSummary({ personKey, n }: { personKey: string; n: number }) {
  const { t } = useTranslation();
  const query = useQuery({
    queryKey: ["notable-summary", personKey, n, NOTABLE_DEPENDENT],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/notable/summary", { params: { query: { key: personKey, n } }, signal })),
    staleTime: 60_000,
    placeholderData: (previous) => previous,
  });
  const data = query.data;
  if (query.error && !data)
    return <p className="text-xs text-muted-foreground" data-person-summary>{t("ui.notable.summary.error")}</p>;
  if (!data) return <Skeleton className="h-9 w-full" />;
  const { buy, sell } = data.sides;
  if (!buy.trades && !sell.trades) return null;
  const missing = buy.after.missing + sell.after.missing;
  const pending = buy.after.pending + sell.after.pending;
  return (
    <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 rounded-lg border bg-card px-3 py-2 text-sm" data-person-summary>
      {(["after", "published"] as const).map((anchor) => (
        <span key={anchor} className="inline-flex flex-wrap items-baseline gap-x-3 gap-y-1" data-summary-anchor={anchor}>
          <span className="text-xs text-muted-foreground">{t(`ui.notable.summary.${anchor}`, { n })}</span>
          <Side stat={buy[anchor]} side="buy" />
          <Side stat={sell[anchor]} side="sell" />
        </span>
      ))}
      <span className="ml-auto inline-flex items-center gap-1 text-xs text-muted-foreground">
        {t("ui.notable.summary.not_advice")}
        <InfoTip>
          <span className="block">{t("ui.notable.summary.rule", { n, small: data.small })}</span>
          <span className="mt-1 block">{t("ui.notable.summary.counted", { trades: buy.trades + sell.trades, missing, pending, no_code: data.no_code })}</span>
        </InfoTip>
      </span>
    </div>
  );
}
