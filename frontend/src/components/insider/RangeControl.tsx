import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { RotateCcw } from "lucide-react";
import { DEFAULT_RANGE, sameRange, todayEt, type Range } from "@/lib/insiderPrefs";
import { cn } from "@/lib/utils";

const MODES = ["months", "count", "dates"] as const;

function NumberInput({ value, onCommit, max, label }: { value: number; onCommit: (value: number) => void; max: number; label: string }) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const commit = () => {
    const number = Number(draft);
    if (Number.isInteger(number) && number >= 1 && number <= max) onCommit(number);
    else setDraft(String(value));
  };
  return (
    <input
      inputMode="numeric"
      aria-label={label}
      value={draft}
      onChange={(event) => setDraft(event.target.value.replace(/\D/g, "").slice(0, 3))}
      onBlur={commit}
      onKeyDown={(event) => event.key === "Enter" && commit()}
      className="h-7 w-14 rounded-md border bg-card px-2 text-center tabular-nums outline-none focus-visible:border-ring"
    />
  );
}

/**
 * Range of a company/person page (D15): last 6 months (default), last 10
 * trades, a custom number of months or trades, or custom dates.
 */
export function RangeControl({ range, onChange }: { range: Range; onChange: (range: Range | null) => void }) {
  const { t } = useTranslation();
  const today = todayEt();
  const sixMonths = sameRange(range, DEFAULT_RANGE);
  const tenTrades = sameRange(range, { mode: "count", count: 10 });
  const preset = (active: boolean, label: string, next: Range) => (
    <button
      type="button"
      aria-pressed={active}
      onClick={() => onChange(next)}
      className={cn("h-7 border-r px-2.5 last:border-r-0 hover:bg-accent", active && "bg-primary text-primary-foreground hover:bg-primary")}
    >
      {label}
    </button>
  );
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs" role="group" aria-label={t("ui.range.label")}>
      <span className="text-muted-foreground">{t("ui.range.label")}</span>
      <div className="inline-flex overflow-hidden rounded-md border bg-card">
        {preset(sixMonths, t("ui.range.six_months"), DEFAULT_RANGE)}
        {preset(tenTrades, t("ui.range.ten_trades"), { mode: "count", count: 10 })}
      </div>
      <select
        aria-label={t("ui.range.custom")}
        value={sixMonths || tenTrades ? "" : range.mode}
        onChange={(event) => {
          const mode = event.target.value as (typeof MODES)[number];
          if (mode === "months") onChange({ mode, months: range.mode === "months" ? range.months : 12 });
          else if (mode === "count") onChange({ mode, count: range.mode === "count" ? range.count : 50 });
          else if (mode === "dates") onChange({ mode, start: `${Number(today.slice(0, 4)) - 1}${today.slice(4)}`, end: today });
        }}
        className={cn("h-7 rounded-md border bg-card px-1.5", !(sixMonths || tenTrades) && "border-primary font-medium")}
      >
        <option value="" disabled>{t("ui.range.custom")}</option>
        {MODES.map((mode) => (
          <option key={mode} value={mode}>{t(`ui.range.mode.${mode}`)}</option>
        ))}
      </select>
      {range.mode === "months" && !sixMonths && (
        <span className="inline-flex items-center gap-1">
          {t("ui.range.last")}
          <NumberInput value={range.months} max={120} label={t("ui.range.mode.months")} onCommit={(months) => onChange({ mode: "months", months })} />
          {t("ui.range.months_unit")}
        </span>
      )}
      {range.mode === "count" && !tenTrades && (
        <span className="inline-flex items-center gap-1">
          {t("ui.range.last")}
          <NumberInput value={range.count} max={500} label={t("ui.range.mode.count")} onCommit={(count) => onChange({ mode: "count", count })} />
          {t("ui.range.trades_unit")}
        </span>
      )}
      {range.mode === "dates" && (
        <span className="inline-flex items-center gap-1">
          <input type="date" aria-label={t("ui.range.start")} value={range.start} max={range.end}
            onChange={(event) => event.target.value && onChange({ ...range, start: event.target.value })}
            className="h-7 rounded-md border bg-card px-1.5 tabular-nums" />
          –
          <input type="date" aria-label={t("ui.range.end")} value={range.end} min={range.start} max={today}
            onChange={(event) => event.target.value && onChange({ ...range, end: event.target.value })}
            className="h-7 rounded-md border bg-card px-1.5 tabular-nums" />
        </span>
      )}
      {!sixMonths && (
        <button type="button" onClick={() => onChange(null)} className="inline-flex h-7 items-center gap-1 rounded-md px-1.5 text-muted-foreground hover:bg-accent hover:text-foreground">
          <RotateCcw className="size-3.5" />
          {t("ui.range.reset")}
        </button>
      )}
    </div>
  );
}
