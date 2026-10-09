import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { LoaderCircle, Play, X } from "lucide-react";
import { BENCHMARKS, DEFAULT_YEARS, MAX_TICKERS, benchmarkName, monthName, parseTickers, type SeasonalKind } from "@/lib/analysis";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

export type ConditionValues = {
  tickers: string[];
  years: number;
  benchmark: string | null;
  start: string; // MM-DD (interval)
  end: string;
};

export const DEFAULT_CONDITIONS: ConditionValues = { tickers: [], years: DEFAULT_YEARS, benchmark: "^GSPC", start: "09-20", end: "10-15" };
const YEAR_PRESETS = [5, 8, 10, 15];

function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      {children}
      {hint && <div className="text-xs text-muted-foreground">{hint}</div>}
    </div>
  );
}

const input = "h-8 rounded-md border bg-background px-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30";

function MonthDay({ value, onChange, label }: { value: string; onChange: (value: string) => void; label: string }) {
  const [month, day] = value.split("-").map(Number);
  const days = new Date(Date.UTC(2000, month, 0)).getUTCDate();
  const set = (m: number, d: number) => onChange(`${String(m).padStart(2, "0")}-${String(Math.min(d, new Date(Date.UTC(2000, m, 0)).getUTCDate())).padStart(2, "0")}`);
  return (
    <div className="flex gap-1.5" role="group" aria-label={label}>
      <select aria-label={label} value={month} onChange={(event) => set(Number(event.target.value), day)} className={cn(input, "min-w-0 flex-1")}>
        {Array.from({ length: 12 }, (_, index) => (
          <option key={index + 1} value={index + 1}>{monthName(index + 1)}</option>
        ))}
      </select>
      <select aria-label={label} value={day} onChange={(event) => set(month, Number(event.target.value))} className={cn(input, "w-16")}>
        {Array.from({ length: days }, (_, index) => (
          <option key={index + 1} value={index + 1}>{index + 1}</option>
        ))}
      </select>
    </div>
  );
}

/**
 * Research conditions: up to 20 tickers, this year plus n complete past years,
 * the benchmark (S&P 500 by default, or none) and, for an interval, its
 * start and end month-day. Nothing is fetched until the reader runs it.
 */
export function Conditions({
  kind,
  initial,
  running,
  onRun,
}: {
  kind: SeasonalKind;
  initial: ConditionValues;
  running: boolean;
  onRun: (values: ConditionValues) => void;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState(initial.tickers.join(", "));
  const [years, setYears] = useState(initial.years);
  const [benchmark, setBenchmark] = useState<string | null>(initial.benchmark);
  const [other, setOther] = useState(initial.benchmark && !(BENCHMARKS as readonly string[]).includes(initial.benchmark) ? initial.benchmark : "");
  const [start, setStart] = useState(initial.start);
  const [end, setEnd] = useState(initial.end);
  const identity = JSON.stringify(initial);
  useEffect(() => {
    // Opening another research shows its conditions.
    const value = JSON.parse(identity) as ConditionValues;
    setText(value.tickers.join(", "));
    setYears(value.years);
    setBenchmark(value.benchmark);
    setOther(value.benchmark && !(BENCHMARKS as readonly string[]).includes(value.benchmark) ? value.benchmark : "");
    setStart(value.start);
    setEnd(value.end);
  }, [identity]);
  const tickers = parseTickers(text);
  const tooMany = tickers.length > MAX_TICKERS;
  const current = new Date().getFullYear();
  const choice = benchmark === null ? "none" : (BENCHMARKS as readonly string[]).includes(benchmark) ? benchmark : "other";
  const chosenBenchmark = choice === "other" ? other.trim().toUpperCase() || null : benchmark;
  const valid = tickers.length > 0 && !tooMany && years >= 1 && years <= 30 && (kind === "monthly" || start !== end) && !(choice === "other" && !other.trim());
  return (
    <form
      className="space-y-4 p-3"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid) onRun({ tickers, years, benchmark: chosenBenchmark, start, end });
      }}
    >
      <Field
        label={t("ui.analysis.form.tickers")}
        hint={<span className={cn(tooMany && "text-destructive")}>{t("ui.analysis.form.tickers_hint", { count: tickers.length, max: MAX_TICKERS })}</span>}
      >
        <textarea
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
          rows={2}
          placeholder="MSFT, AAPL, NVDA"
          aria-label={t("ui.analysis.form.tickers")}
          className={cn(input, "h-auto w-full resize-y py-1.5 font-medium uppercase placeholder:normal-case placeholder:font-normal")}
        />
        {tickers.length > 1 && (
          <div className="flex flex-wrap gap-1">
            {tickers.map((ticker) => (
              <span key={ticker} className="inline-flex items-center gap-0.5 rounded bg-secondary py-0.5 pr-0.5 pl-1.5 text-xs font-semibold tabular-nums">
                {ticker}
                <button
                  type="button"
                  aria-label={t("ui.analysis.form.remove", { ticker })}
                  onClick={() => setText(tickers.filter((item) => item !== ticker).join(", "))}
                  className="grid size-4 place-items-center rounded text-muted-foreground hover:bg-background hover:text-foreground"
                >
                  <X className="size-3" />
                </button>
              </span>
            ))}
          </div>
        )}
      </Field>

      {kind === "interval" && (
        <Field
          label={t("ui.analysis.form.interval")}
          hint={end < start ? t("ui.analysis.form.cross_year") : start === end ? <span className="text-destructive">{t("ui.analysis.form.same_day")}</span> : null}
        >
          <div className="space-y-1.5">
            <MonthDay value={start} onChange={setStart} label={t("ui.analysis.form.start")} />
            <MonthDay value={end} onChange={setEnd} label={t("ui.analysis.form.end")} />
          </div>
        </Field>
      )}

      <Field label={t("ui.analysis.form.years")} hint={t("ui.analysis.form.years_hint", { first: current - years, last: current - 1, current })}>
        <div className="flex items-center gap-1.5">
          <div className="inline-flex overflow-hidden rounded-md border">
            {YEAR_PRESETS.map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={years === value}
                onClick={() => setYears(value)}
                className={cn("h-8 min-w-8 border-r px-2 text-sm tabular-nums last:border-r-0 hover:bg-accent", years === value && "bg-primary text-primary-foreground hover:bg-primary")}
              >
                {value}
              </button>
            ))}
          </div>
          <input
            type="number"
            min={1}
            max={30}
            value={years}
            aria-label={t("ui.analysis.form.years_custom")}
            onChange={(event) => setYears(Math.max(1, Math.min(30, Number(event.target.value) || 1)))}
            className={cn(input, "w-14 text-center tabular-nums", !YEAR_PRESETS.includes(years) && "border-primary font-medium")}
          />
        </div>
      </Field>

      <Field label={t("ui.analysis.form.benchmark")}>
        <div className="space-y-1" role="radiogroup" aria-label={t("ui.analysis.form.benchmark")}>
          {[...BENCHMARKS, "none", "other"].map((value) => (
            <label key={value} className="flex h-7 cursor-pointer items-center gap-2 text-sm">
              <input
                type="radio"
                name="benchmark"
                checked={choice === value}
                onChange={() => setBenchmark(value === "none" ? null : value === "other" ? other.trim().toUpperCase() || "OTHER" : value)}
                className="accent-primary"
              />
              {value === "other" ? (
                <input
                  value={other}
                  placeholder={t("ui.analysis.form.benchmark_other")}
                  aria-label={t("ui.analysis.form.benchmark_other")}
                  onFocus={() => setBenchmark(other.trim().toUpperCase() || "OTHER")}
                  onChange={(event) => {
                    setOther(event.target.value.toUpperCase());
                    setBenchmark(event.target.value.trim().toUpperCase() || "OTHER");
                  }}
                  className={cn(input, "h-7 w-full uppercase placeholder:normal-case")}
                />
              ) : value === "none" ? (
                t("ui.analysis.form.benchmark_none")
              ) : (
                benchmarkName(value)
              )}
            </label>
          ))}
        </div>
      </Field>

      <Button type="submit" className="w-full" disabled={!valid || running}>
        {running ? <LoaderCircle className="motion-safe:animate-spin" /> : <Play />}
        {t("ui.analysis.form.run")}
      </Button>
    </form>
  );
}
