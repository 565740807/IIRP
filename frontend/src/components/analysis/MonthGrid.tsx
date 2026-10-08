import { useTranslation } from "react-i18next";
import { monthName, pct, type Period } from "@/lib/analysis";
import { cn } from "@/lib/utils";

/** Cell tint: blue up, orange down; stronger with size, full at ±10%. */
function tint(value: string | null | undefined) {
  if (value == null) return undefined;
  const n = Number(value);
  const strength = Math.min(Math.abs(n) / 0.1, 1);
  const color = n >= 0 ? "var(--up)" : "var(--down)";
  return { backgroundColor: `color-mix(in oklab, ${color} ${Math.round(6 + strength * 38)}%, transparent)` };
}

/**
 * Year × month table of open → close changes; the median and up count of the
 * complete past years in the bottom rows. Clicking a month chooses it.
 */
export function MonthGrid({ periods, selected, onSelect }: { periods: Period[]; selected: number; onSelect: (month: number) => void }) {
  const { t } = useTranslation();
  const years = periods[0]?.years.map((candle) => candle.year).slice().reverse() ?? [];
  return (
    <div className="overflow-x-auto rounded-lg border bg-card">
      <table className="w-full table-fixed border-collapse text-xs tabular-nums">
        <colgroup>
          <col className="w-14" />
          {periods.map((period) => (
            <col key={period.key} />
          ))}
        </colgroup>
        <thead>
          <tr className="border-b">
            <th className="px-2 py-1.5 text-left font-medium text-muted-foreground">{t("ui.analysis.col.year")}</th>
            {periods.map((period) => (
              <th key={period.key} className="p-0 font-medium">
                <button
                  type="button"
                  onClick={() => onSelect(period.month!)}
                  aria-pressed={selected === period.month}
                  className={cn(
                    "w-full px-1 py-1.5 text-center text-muted-foreground hover:text-foreground",
                    selected === period.month && "bg-primary text-primary-foreground hover:text-primary-foreground",
                  )}
                >
                  {monthName(period.month!, "short")}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {years.map((year) => (
            <tr key={year} className="border-b border-border/60">
              <th className="px-2 py-1 text-left font-medium text-muted-foreground">{year}</th>
              {periods.map((period) => {
                const candle = period.years.find((item) => item.year === year)!;
                const progress = candle.status === "in_progress";
                return (
                  <td key={period.key} className={cn("p-0", selected === period.month && "outline outline-1 -outline-offset-1 outline-primary/40")}>
                    <button
                      type="button"
                      onClick={() => onSelect(period.month!)}
                      title={candle.change == null ? t(`ui.analysis.status.${candle.status}`) : progress ? t("ui.analysis.status.in_progress") : undefined}
                      className={cn("h-7 w-full px-1 text-center hover:ring-1 hover:ring-ring", progress && "italic", candle.change == null && "text-muted-foreground/60")}
                      style={tint(candle.change)}
                    >
                      {candle.change == null ? (candle.status === "not_started" ? "" : "·") : pct(candle.change)}
                      {progress && <span className="align-super text-[9px]">*</span>}
                    </button>
                  </td>
                );
              })}
            </tr>
          ))}
          <tr className="border-t-2 font-medium">
            <th className="px-2 py-1 text-left text-muted-foreground">{t("ui.analysis.col.median")}</th>
            {periods.map((period) => (
              <td key={period.key} className="px-1 py-1 text-center" style={tint(period.stats.median)}>
                {pct(period.stats.median)}
              </td>
            ))}
          </tr>
          <tr>
            <th className="px-2 py-1 text-left font-medium text-muted-foreground">{t("ui.analysis.col.up_short")}</th>
            {periods.map((period) => (
              <td key={period.key} className="px-1 py-1 text-center text-muted-foreground">
                {period.stats.n ? `${period.stats.up}/${period.stats.n}` : "—"}
              </td>
            ))}
          </tr>
        </tbody>
      </table>
    </div>
  );
}
