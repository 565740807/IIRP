import { useTranslation } from "react-i18next";
import { formatDay, formatEt } from "@/lib/format";
import type { Period, SectorChange, SectorItem } from "@/lib/sectors";

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
export function SinceNote({ item, years }: { item: SectorItem; years?: number }) {
  const { t } = useTranslation();
  const target = years ?? 8;
  if (!item.first_date || item.full_years >= target) return null;
  return (
    <span className="text-[11px] text-muted-foreground">
      {t("ui.sectors.since", { day: formatDay(item.first_date, { year: true }), years: item.full_years, target })}
    </span>
  );
}
