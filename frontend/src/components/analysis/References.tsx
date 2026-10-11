import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { BENCHMARKS, benchmarkName, type Basis, type SectorReference } from "@/lib/analysis";
import { tm } from "@/i18n";

/** Each control keeps an unfinished custom ticker local until it is valid. */
export function ReferenceChoice({ value, onChange, sector = false, onValidity }: { value: string | null; onChange: (value: string | null) => void; sector?: boolean; onValidity?: (valid: boolean) => void }) {
  const { t } = useTranslation();
  const presets: readonly string[] = sector ? ["auto", "industry_group"] : BENCHMARKS;
  const choiceOf = (v: string | null) => v === null ? "none" : presets.includes(v) ? v : "other";
  const [choice, setChoice] = useState(choiceOf(value));
  const [other, setOther] = useState(choiceOf(value) === "other" ? value ?? "" : "");
  useEffect(() => { setChoice(choiceOf(value)); if (choiceOf(value) === "other") setOther(value ?? ""); }, [value, sector]); // eslint-disable-line react-hooks/exhaustive-deps
  const label = t(sector ? "ui.analysis.form.sector_benchmark" : "ui.analysis.form.benchmark");
  const commit = () => { if (other.trim()) onChange(other.trim().toUpperCase()); };
  return <div className="space-y-1.5">
    <div className="text-xs font-medium text-muted-foreground">{label}</div>
    <div className="space-y-1" role="radiogroup" aria-label={label}>
      {[...presets, "none", "other"].map((key) => <label key={key} className="flex min-h-7 cursor-pointer items-center gap-2 text-sm">
        <input type="radio" name={sector ? "sector-benchmark" : "market-benchmark"} checked={choice === key} className="accent-primary" onChange={() => {
          setChoice(key); onValidity?.(key !== "other" || !!other.trim());
          if (key === "none") onChange(null); else if (key !== "other") onChange(key); else commit();
        }} />
        {key === "other" ? <input value={other} placeholder={t("ui.analysis.form.benchmark_other")} aria-label={t(sector ? "ui.analysis.form.sector_other" : "ui.analysis.form.benchmark_other")} className="h-7 min-w-0 w-full rounded-md border bg-background px-2 text-sm uppercase outline-none placeholder:normal-case focus-visible:border-ring" onFocus={() => { setChoice("other"); onValidity?.(!!other.trim()); }} onChange={(e) => { setOther(e.target.value.toUpperCase()); onValidity?.(!!e.target.value.trim()); }} onBlur={commit} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } }} /> : key === "none" ? t("ui.analysis.form.benchmark_none") : sector ? t(`ui.analysis.sector.choice.${key}`) : benchmarkName(key)}
      </label>)}
    </div>
    {sector && <p className="text-xs text-muted-foreground">{t("ui.analysis.sector.hint")}</p>}
  </div>;
}

export function BasisChoice({ value, onChange, benchmark, sector }: { value: Basis; onChange: (value: Basis) => void; benchmark: boolean; sector: boolean }) {
  const { t } = useTranslation();
  return <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">{t("ui.analysis.basis.label")}<select aria-label={t("ui.analysis.basis.label")} value={value} onChange={(e) => onChange(e.target.value as Basis)} className="h-7 rounded border bg-card px-2 text-foreground">
    {(["absolute", "market", "sector"] as const).map((key) => <option key={key} value={key} disabled={key === "market" && !benchmark || key === "sector" && !sector}>{t(`ui.analysis.basis.${key}`)}</option>)}
  </select></label>;
}

export function SectorBadge({ reference, selected = false }: { reference?: SectorReference | null; selected?: boolean }) {
  const { t } = useTranslation();
  if (!reference) return null;
  return <span className={`ml-1.5 inline-flex items-center gap-1 text-xs font-normal ${selected ? "text-current" : "text-muted-foreground"}`} title={reference.reason ? tm(reference.reason) : t(`ui.analysis.sector.source.${reference.source}`)}>{reference.symbol ?? t("ui.analysis.sector.source.none")} · {t(`ui.analysis.sector.source.${reference.source}`)}</span>;
}

export function SectorNote({ reference, included }: { reference?: SectorReference | null; included?: boolean }) {
  const { t } = useTranslation();
  if (!included) return <p className="rounded-md border bg-card px-3 py-2 text-xs text-muted-foreground">{t("ui.analysis.sector.legacy")}</p>;
  if (!reference) return null;
  return <p className="rounded-md border bg-card px-3 py-2 text-xs text-muted-foreground">{t("ui.analysis.form.sector_benchmark")}<SectorBadge reference={reference} />{reference.fallback && <span> · {t("ui.analysis.sector.fallback")}</span>}{reference.reason && <span> · {tm(reference.reason)}</span>}</p>;
}
