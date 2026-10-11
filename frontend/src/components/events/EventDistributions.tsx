import { useState } from "react";
import { useTranslation } from "react-i18next";
import { BoxPlot, type BoxGroup } from "@/components/analysis/BoxPlot";
import { UpShare } from "@/components/analysis/UpShare";
import { Signed } from "@/components/analysis/Tables";
import { distribution, type Basis, benchmarkName } from "@/lib/analysis";
import { BasisChoice } from "@/components/analysis/References";
import { WINDOWS, windowLabel, type EventResult, type EventRow, type WindowKey } from "@/lib/events";
import { formatDay } from "@/lib/format";

const observations = (rows: EventRow[], window: WindowKey, basis: Basis) => rows.map((row) => ({ id: row.id, label: `${row.name} · ${formatDay(row.date, { year: true })}`, value: basis === "market" ? row.windows[window].excess : basis === "sector" ? row.windows[window].sector_excess : row.windows[window].value }));
const group = (key: string, label: string, result: EventResult, window: WindowKey, rows = result.rows, stats = result.summary[window], basis: Basis = "absolute"): BoxGroup => ({ key, label, stats: distribution(stats, basis), benchmark: stats.benchmark_box, points: observations(rows, window, basis) });

export function WindowCards({ result, selected, onSelect, basis = "absolute" }: { result: EventResult; basis?: Basis; selected: string | null; onSelect: (id: string) => void }) {
  const { t } = useTranslation();
  return <div className="grid grid-cols-2 gap-2 xl:grid-cols-4">{WINDOWS.map((window) => {
    const absolute = result.summary[window];
    const stats = distribution(absolute, basis);
    return <section key={window} className="min-w-0 rounded-lg border bg-card px-3 pt-3 pb-2">
      <h2 className="text-xs font-semibold">{windowLabel(window, result.n)}</h2>
      <div className="mt-2 flex items-baseline gap-2"><span className="text-xs text-muted-foreground">{t("ui.analysis.col.median")}</span><span className="text-lg font-semibold"><Signed value={stats.median} /></span></div>
      <p className="text-xs"><span className="text-muted-foreground">{t("ui.analysis.col.mean")} </span><Signed value={stats.mean} /></p>
      <div className="mt-2 flex flex-col text-sm"><span className="text-xs text-muted-foreground">{t(basis === "absolute" ? "ui.events.col.up" : "ui.analysis.basis.absolute_up")}</span><UpShare stats={absolute} /></div>
      <BoxPlot groups={[group(window, windowLabel(window, result.n), result, window, result.rows, absolute, basis)]} title={windowLabel(window, result.n)} selected={selected} onSelect={onSelect} compact height={72} />
    </section>;
  })}</div>;
}

export function EventDistributions({ result, results, benchmark, selected, onSelect, basis, setBasis }: { basis: Basis; setBasis: (basis: Basis) => void; result: EventResult; results: EventResult[]; benchmark: string | null; selected: string | null; onSelect: (id: string) => void }) {
  const { t } = useTranslation();
  const [window, setWindow] = useState<WindowKey>("reaction");
  const [compare, setCompare] = useState(false);
  const control = <div className="flex flex-wrap items-center gap-2">
    <label className="text-xs text-muted-foreground">{t("ui.events.col.window")} <select aria-label={t("ui.events.col.window")} value={window} onChange={(e) => setWindow(e.target.value as WindowKey)} className="h-7 rounded border bg-card px-2 text-foreground">{WINDOWS.map((key) => <option key={key} value={key}>{windowLabel(key, result.n)}</option>)}</select></label>
    <BasisChoice value={basis} onChange={setBasis} benchmark={!!benchmark} sector={!!result.sector?.symbol} />
    {basis === "absolute" && benchmark && <label className="inline-flex items-center gap-1 text-xs"><input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} />{t("ui.events.show_benchmark", { benchmark: benchmarkName(benchmark) })}</label>}
  </div>;
  const benchmarkLabel = basis === "absolute" && compare && benchmark ? benchmarkName(benchmark) : undefined;
  return <section className="space-y-2">
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold">{t("ui.events.box.windows")}</h2>{control}</div>
    <div className="rounded-lg border bg-card"><BoxPlot groups={WINDOWS.map((key) => group(key, windowLabel(key, result.n), result, key, result.rows, result.summary[key], basis))} title={t("ui.events.box.windows")} selected={selected} onSelect={onSelect} benchmarkLabel={benchmarkLabel} /></div>
    <p className="text-xs text-muted-foreground">{t("ui.events.box.legend")}</p>
    {result.quarters?.length ? <><h2 className="text-sm font-semibold">{t("ui.events.box.quarters")} · {windowLabel(window, result.n)}</h2><div className="rounded-lg border bg-card"><BoxPlot groups={result.quarters.map((q) => group(`Q${q.fiscal_quarter}`, `Q${q.fiscal_quarter}`, result, window, result.rows.filter((r) => r.fiscal_quarter === q.fiscal_quarter), q.summary[window], basis))} title={t("ui.events.box.quarters")} selected={selected} onSelect={onSelect} benchmarkLabel={benchmarkLabel} /></div></> : null}
    {results.length > 1 && <><h2 className="text-sm font-semibold">{t("ui.events.box.tickers")} · {windowLabel(window, result.n)}</h2><div className="rounded-lg border bg-card"><BoxPlot groups={results.map((r) => group(r.symbol, r.symbol, r, window, r.rows, r.summary[window], basis))} title={t("ui.events.box.tickers")} selected={selected} onSelect={onSelect} benchmarkLabel={benchmarkLabel} /></div></>}
  </section>;
}
