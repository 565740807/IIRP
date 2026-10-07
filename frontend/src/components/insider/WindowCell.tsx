import { useTranslation } from "react-i18next";
import { LoaderCircle } from "lucide-react";
import { tm } from "@/i18n";
import { directionClass, formatDay, formatSignedRatio } from "@/lib/format";
import type { TickerPrices } from "@/lib/windows";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

type Change = { value?: string | null; start_date?: string | null; end_date?: string | null; status: string };
type Prices = Pick<TickerPrices, "status"> & { reason?: string | null };

/**
 * One before- or after-n change: signed and colored (blue up, orange down);
 * empty with the expected date when that session has not come yet, "intraday"
 * before today's close, a spinner while prices are being fetched.
 */
export function WindowCell({
  change,
  ticker,
  side,
  n,
  className,
}: {
  change: Change | null | undefined;
  ticker: Prices | undefined;
  side: "before" | "after";
  n: number;
  className?: string;
}) {
  const { t } = useTranslation();
  const range = change?.start_date && change?.end_date
    ? t("ui.window.range", { start: formatDay(change.start_date), end: formatDay(change.end_date) })
    : "";
  const base = cn("inline-flex justify-end tabular-nums", className);
  if (change?.status === "available")
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className={cn(base, "cursor-default font-medium", directionClass(change.value))}>{formatSignedRatio(change.value)}</span>
        </TooltipTrigger>
        <TooltipContent>{t(`ui.window.${side}_tip`, { n, range })}</TooltipContent>
      </Tooltip>
    );
  if (ticker?.status === "fetching" || (!change && ticker?.status === "not_fetched"))
    return (
      <span className={cn(base, "text-muted-foreground")} aria-label={t("ui.window.fetching")}>
        <LoaderCircle className="size-3 motion-safe:animate-spin" />
      </span>
    );
  const label = change?.status === "intraday" ? t("ui.window.intraday") : "—";
  const reason =
    change?.status === "pending"
      ? t("ui.window.pending", { day: formatDay(side === "after" ? change.end_date : change.start_date, { year: true }) })
      : change?.status === "intraday"
        ? t("ui.window.intraday_tip")
        : ticker?.status === "unavailable"
          ? tm(ticker.reason) || t("ui.window.unavailable")
          : change?.status === "missing"
            ? t("ui.window.missing", { range })
            : ticker?.status === "not_fetched" || change?.status === "no_prices"
              ? t("ui.window.not_fetched")
              : t("ui.window.no_ticker");
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className={cn(base, "cursor-default text-muted-foreground", change?.status === "intraday" && "text-[11px] font-medium text-warn")}>{label}</span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">{reason}</TooltipContent>
    </Tooltip>
  );
}
