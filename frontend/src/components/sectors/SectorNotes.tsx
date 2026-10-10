import { useTranslation } from "react-i18next";
import { CircleAlert, LoaderCircle } from "lucide-react";
import { tm } from "@/i18n";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatDay, formatEt } from "@/lib/format";
import type { Period, PriceFetch, SectorChange } from "@/lib/sectors";

/** Which closes a change compares, e.g. "Sep 3 close → Oct 9 close". */
export function ChangeBasis({ change, period }: { change: SectorChange & { mode?: string; as_of?: string | null }; period: Period }) {
  const { t } = useTranslation();
  if (!change.start_date || !change.end_date) return null;
  if (period === "today" && change.mode === "intraday") {
    return <p>{t("ui.sectors.basis_intraday", { start: formatDay(change.start_date), time: formatEt(change.as_of) })}</p>;
  }
  return <p>{t("ui.sectors.basis_close", { start: formatDay(change.start_date, { year: true }), end: formatDay(change.end_date, { year: true }) })}</p>;
}

/** For ETFs that started inside the history window: since when, and how many full years. */
export function SinceNote({ item, years }: { item: { first_date?: string | null; full_years: number }; years?: number }) {
  const { t } = useTranslation();
  const target = years ?? 8;
  if (!item.first_date || item.full_years >= target) return null;
  return (
    <span className="text-[11px] text-muted-foreground">
      {t("ui.sectors.since", { day: formatDay(item.first_date, { year: true }), years: item.full_years, target })}
    </span>
  );
}

/** A fetch in progress, or the reason an ETF has no daily prices; nothing once ready. */
export function FetchMark({ fetch }: { fetch?: PriceFetch | null }) {
  const { t } = useTranslation();
  if (!fetch || fetch.status === "ready") return null;
  if (fetch.status === "fetching" || fetch.status === "not_requested") {
    return <LoaderCircle className="size-3 text-muted-foreground motion-safe:animate-spin" aria-label={t("ui.sectors.fetch.fetching")}/>;
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex cursor-default items-center gap-0.5 font-sans text-[11px] text-warn">
          <CircleAlert className="size-3" aria-hidden/>{t(`ui.sectors.fetch.${fetch.status}`)}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">{fetch.reason ? tm(fetch.reason) : t(`ui.sectors.fetch_rule.${fetch.status}`)}</TooltipContent>
    </Tooltip>
  );
}

/** Daily-price progress of each ETF of the level, while any is not ready or has none. */
export function FetchProgress({ rows }: { rows: readonly { etf: string; fetch?: PriceFetch | null }[] }) {
  const { t } = useTranslation();
  const ready = rows.filter((row) => row.fetch?.status === "ready").length;
  if (ready === rows.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground" role="status">
      <span>{t("ui.sectors.fetch_progress", { ready, total: rows.length })}</span>
      {rows.filter((row) => row.fetch?.status !== "ready").map((row) => (
        <span key={row.etf} className="inline-flex items-center gap-1 rounded border px-1.5 py-px font-mono">
          {row.etf}<FetchMark fetch={row.fetch}/>
        </span>
      ))}
    </div>
  );
}
