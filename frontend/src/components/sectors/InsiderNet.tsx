import { useTranslation } from "react-i18next";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { directionClass, formatDay, formatMoney } from "@/lib/format";
import { usePageVisible, useRefreshIntervals } from "@/lib/refresh";
import { cn } from "@/lib/utils";
import { InfoTip } from "@/components/ui/info";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

export type InsiderSectors = Schemas["SectorActivityOutput"];
type Totals = Schemas["SectorActivityTotals"] & { ungrouped?: number };

/** Days of the sector tables' Insider column. */
export const INSIDER_DAYS = 30;

/** Open-market Insider totals by saved sector and industry group (local reads only), at the overview's pace. */
export function useInsiderSectors(enabled: boolean) {
  const visible = usePageVisible();
  const intervals = useRefreshIntervals();
  return useQuery({
    queryKey: ["insider-sectors", INSIDER_DAYS],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/insider/sectors", { params: { query: { days: INSIDER_DAYS } }, signal })),
    enabled,
    refetchInterval: enabled && visible ? intervals.overview_poll_seconds * 1000 : false,
    placeholderData: keepPreviousData,
  });
}

/** The net amount a row sorts by; rows without any trade sort last. */
export function insiderNet(totals: Totals | undefined) {
  return totals && totals.companies > 0 && totals.net_amount != null ? Number(totals.net_amount) : undefined;
}

/** Column header text; the ⓘ with what is counted sits outside the sort button (InsiderInfo). */
export function InsiderHeader() {
  const { t } = useTranslation();
  return <>{t("ui.sectors.column.insider", { count: INSIDER_DAYS })}</>;
}

export function InsiderInfo() {
  const { t } = useTranslation();
  return <InfoTip className="ml-0.5">{t("ui.sectors.insider_rule", { count: INSIDER_DAYS })}</InfoTip>;
}

/**
 * Net open-market purchases minus sales of the row's companies; hover gives
 * both sides, how many companies, and the companies left out for want of a
 * classification. ``group`` rows count only companies with a GICS sub-industry.
 */
export function InsiderNetCell({ totals, data, group }: { totals: Totals | undefined; data: InsiderSectors | undefined; group?: { sector: string } }) {
  const { t } = useTranslation();
  if (!data) return <span className="text-muted-foreground">—</span>;
  const net = insiderNet(totals);
  const sectorRow = group ? data.sectors.find((item) => item.id === group.sector) : undefined;
  const ungrouped = group ? sectorRow?.ungrouped ?? 0 : 0;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span data-insider-net className={cn("inline-block cursor-default rounded px-1 tabular-nums", net == null ? "text-muted-foreground" : directionClass(net))}>
          {net == null ? "—" : formatMoney(net, { signed: true })}
          {totals && totals.missing_price_rows > 0 && <span className="text-warn">*</span>}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs flex-col items-start gap-1">
        <p className="font-medium">{t("ui.sectors.insider_window", { start: formatDay(data.start_date), end: formatDay(data.end_date) })}</p>
        {totals && totals.companies > 0 ? <>
          {totals.buy_companies > 0 && <p className="tabular-nums">{t("ui.sectors.insider_buy", { amount: formatMoney(totals.buy_amount), count: totals.buy_companies })}</p>}
          {totals.sell_companies > 0 && <p className="tabular-nums">{t("ui.sectors.insider_sell", { amount: formatMoney(totals.sell_amount), count: totals.sell_companies })}</p>}
          <p>{t("ui.sectors.insider_companies", { count: totals.companies })}</p>
        </> : <p>{t("ui.sectors.insider_none")}</p>}
        {totals && totals.missing_price_rows > 0 && <p>* {t("ui.overview.partial_amount")}</p>}
        {totals && totals.price_mismatch_rows > 0 && <p>{t("ui.overview.mismatch_excluded", { count: totals.price_mismatch_rows })}</p>}
        {group && ungrouped > 0 && <p className="opacity-75">{t("ui.sectors.insider_ungrouped", { count: ungrouped })}</p>}
        {data.unclassified.companies > 0 && <p className="opacity-75">{t("ui.sectors.insider_unclassified", { count: data.unclassified.companies })}</p>}
      </TooltipContent>
    </Tooltip>
  );
}
