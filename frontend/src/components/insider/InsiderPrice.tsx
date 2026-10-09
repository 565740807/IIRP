import { createContext, useContext } from "react";
import { useTranslation } from "react-i18next";
import { formatCompact, formatDay, formatPrice, formatSignedPercent, directionClass } from "@/lib/format";
import type { PriceComparison } from "@/lib/insiderOverview";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/** Ratio from which the backend marks a reported price as a mismatch; it comes with each response. */
export const MismatchRatio = createContext<string | null>(null);

/** The backend's check of a price against the market: mismatch or not yet checked. */
function PriceCheckTag({ price }: { price?: PriceComparison | null }) {
  const { t } = useTranslation();
  if (price?.price == null) return null;
  if (price.price_check === "mismatch") {
    return <span className="mr-1 rounded bg-secondary px-1 text-[10px] text-warn">
      {t("ui.overview.price_check.mismatch", { ratio: formatCompact(price.mismatch_ratio) })}
    </span>;
  }
  if (price.price_check === "unchecked") {
    return <span className="mr-1 text-[10px] text-muted-foreground">
      {t("ui.overview.price_check.unchecked")}
    </span>;
  }
  return null;
}

function PriceCheckNote({ price }: { price?: PriceComparison | null }) {
  const { t } = useTranslation();
  const limit = useContext(MismatchRatio);
  // Missing and $0 prices (option exercises, awards) have nothing to check.
  if (price?.price == null || price.price_check === "not_applicable") return null;
  const ratio = formatCompact(price.mismatch_ratio);
  return <>
    {price.price_check === "mismatch" && <p>{t("ui.overview.price_check.mismatch_rule", { ratio })}</p>}
    {price.price_check === "unchecked" && <p>{t("ui.overview.price_check.unchecked_rule")}</p>}
    {limit != null && <p className="text-muted-foreground">{t("ui.overview.price_check.rule", { ratio: formatCompact(limit) })}</p>}
  </>;
}

export function TradePrice({ price, average = false }: { price?: PriceComparison | null; average?: boolean }) {
  const { t } = useTranslation();
  const range = price?.range_low != null && price.range_high != null;
  const rangeKey = price?.estimated ? "ui.overview.day_range" : "ui.overview.weighted_range";
  return <Tooltip>
    <TooltipTrigger asChild>
      <span className="cursor-default whitespace-nowrap tabular-nums">
        <PriceCheckTag price={price}/>
        {price?.estimated && <span className="mr-1 text-[10px] text-muted-foreground">{t("ui.overview.estimated")}</span>}
        {formatPrice(price?.price)}
      </span>
    </TooltipTrigger>
    <TooltipContent className="max-w-xs">
      {average && <p>{t("ui.overview.average_method")}</p>}
      <p>{t(price?.estimated ? "ui.overview.estimate_method" : "ui.overview.reported_price")}</p>
      {range && <p>{t(rangeKey, { low: formatPrice(price.range_low), high: formatPrice(price.range_high) })}</p>}
      <PriceCheckNote price={price}/>
    </TooltipContent>
  </Tooltip>;
}

export function CurrentPrice({ price, compact = false }: { price?: PriceComparison | null; compact?: boolean }) {
  const { t } = useTranslation();
  return <Tooltip>
    <TooltipTrigger asChild>
      <span className={cn("inline-flex cursor-default items-baseline justify-end gap-1 whitespace-nowrap tabular-nums", compact ? "text-[11px]" : "text-xs")}>
        <span>{formatPrice(price?.current_price)}</span>
        {price?.change_percent != null && <span className={directionClass(price.change_percent)}>{formatSignedPercent(price.change_percent)}</span>}
        {price?.relation && <span className="rounded bg-secondary px-1 text-[10px] text-muted-foreground">{t(`ui.overview.relation.${price.relation}`)}</span>}
      </span>
    </TooltipTrigger>
    <TooltipContent className="max-w-xs">
      <p>{t("ui.overview.comparison_rule")}</p>
      {price?.estimated && <p>{t("ui.overview.comparison_estimated")}</p>}
      {price?.quote_status && <p>{t(`ui.overview.quote_status.${price.quote_status}`, { day: formatDay(price.quote_date, { year: true }) })}</p>}
    </TooltipContent>
  </Tooltip>;
}

/** Saved close dates stay visible without doubling the height of each trade row. */
export function CurrentQuoteStatus({ prices }: { prices: readonly PriceComparison[] }) {
  const { t } = useTranslation();
  const closed = [...new Set(prices.filter((price) => price.quote_status === "closed").map((price) => price.quote_date).filter((day): day is string => !!day))].sort();
  const delayed = [...new Set(prices.filter((price) => price.quote_status === "delayed").map((price) => price.quote_date).filter((day): day is string => !!day))].sort();
  return <>
    {closed.length > 0 && <span className="text-[11px] text-muted-foreground">{t("ui.overview.quote_status.closed", { day: closed.map((day) => formatDay(day, { year: true })).join(" · ") })}</span>}
    {delayed.length > 0 && <span className="text-[11px] text-warn">{t("ui.overview.quote_status.delayed", { day: delayed.map((day) => formatDay(day, { year: true })).join(" · ") })}</span>}
  </>;
}
