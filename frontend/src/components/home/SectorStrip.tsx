import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import NumberFlow from "@number-flow/react";
import { locale } from "@/i18n";
import { formatDay, formatEt, formatLocal } from "@/lib/format";
import { sourceContext } from "@/lib/navigation";
import { useSectors, type SectorItem, type SectorPerformance } from "@/lib/sectors";
import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useFlash } from "./MarketStrip";

function SectorTile({ item }: { item: SectorItem }) {
  const { t } = useTranslation();
  const location = useLocation();
  const raw = item.periods.today.value;
  const value = raw == null ? null : Number(raw);
  const flash = useFlash(value);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Link
          to="/sectors"
          state={sourceContext(location)}
          className={cn(
            "flex min-w-0 flex-col gap-0.5 px-2.5 py-1.5 transition-colors duration-1000 hover:bg-accent/60",
            flash === "up" && "bg-up-flash duration-150",
            flash === "down" && "bg-down-flash duration-150",
          )}
        >
          <span className="truncate text-[11px] text-muted-foreground">{t(`ui.sectors.short.${item.id}`)}</span>
          <span className={cn("text-xs font-semibold tabular-nums", value == null ? "text-muted-foreground" : value > 0 ? "text-up" : value < 0 ? "text-down" : "")}>
            {value == null ? "—" : (
              <NumberFlow
                value={value}
                locales={locale()}
                format={{ style: "percent", minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "exceptZero" }}
              />
            )}
          </span>
        </Link>
      </TooltipTrigger>
      <TooltipContent>{t(`ui.sectors.name.${item.id}`)} · {item.etf}</TooltipContent>
    </Tooltip>
  );
}

/** "Intraday · updated 10:31 AM ET" or "Oct 9 close vs Oct 8 close". */
export function TodayBasis({ data }: { data: { items: readonly { periods?: SectorPerformance["items"][number]["periods"] | null }[] } }) {
  const { t } = useTranslation();
  const today = data.items.map((item) => item.periods?.today).find((change) => change?.value != null);
  if (!today) return <span>{t("ui.sectors.waiting")}</span>;
  if (today.mode === "intraday") {
    const time = data.items.map((item) => item.periods?.today.as_of).filter(Boolean).sort().at(-1);
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex cursor-default items-center gap-1.5">
            <span className="size-1.5 rounded-full bg-up" aria-hidden />
            {t("ui.sectors.intraday", { time: formatEt(time, { date: false }) })}
          </span>
        </TooltipTrigger>
        <TooltipContent>{t("ui.sectors.intraday_rule", { day: formatDay(today.start_date) })} · {t("ui.time.local", { time: formatLocal(time) })}</TooltipContent>
      </Tooltip>
    );
  }
  return <span>{t("ui.sectors.closed_basis", { end: formatDay(today.end_date), start: formatDay(today.start_date) })}</span>;
}

/** Eleven S&P 500 sectors by today's change, under the index strip; opens the sector page. */
export function SectorStrip() {
  const { t } = useTranslation();
  const { query, error } = useSectors();
  const data = query.data;
  return (
    <section aria-label={t("ui.sectors.strip_label")} className="mt-2">
      <div className="grid grid-cols-11 divide-x overflow-hidden rounded-lg border bg-card">
        {!data
          ? Array.from({ length: 11 }, (_, index) => (
              <div key={index} className="flex flex-col gap-1 px-2.5 py-1.5">
                <Skeleton className="h-3 w-14" />
                <Skeleton className="h-3.5 w-10" />
              </div>
            ))
          : data.items.map((item) => <SectorTile key={item.id} item={item} />)}
      </div>
      <p className="flex flex-wrap items-center gap-x-2 px-1 pt-1.5 text-xs text-muted-foreground" role="status">
        <Link to="/sectors" className="font-medium text-foreground hover:underline">{t("ui.sectors.strip_title")}</Link>
        {data && <TodayBasis data={data} />}
        {error && <span className="text-destructive">{error.message}</span>}
      </p>
    </section>
  );
}
