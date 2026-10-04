import type { Fact } from "./api";
import { fiscalVersionNotice } from "./fiscalVersionNotice";

/** Facts copied from the frozen backend result; no price/statistics recalculation. */
export function chartExportContext(
  data: Fact,
  provenance: string,
  notes: string[] = [],
  displayedSample?: string,
): string[] {
  const meta = data.metadata ?? {};
  const summary = data.summary ?? {};
  const params = meta.params ?? {};
  const kind = params.kind ?? data.kind;
  const window = kind === "monthly" ? `${params.month ?? "?"}月`
    : kind === "interval" ? `${params.start_mmdd ?? "?"} → ${params.end_mmdd ?? "?"}`
    : kind === "earnings" ? `Q${params.quarter ?? "?"}`
    : kind === "event_dates" ? `${params.date_window ?? meta.date_window ?? "日期观察"}`
    : String(kind ?? "未知");
  const years = Array.isArray(meta.historical_years) ? meta.historical_years
    : Array.isArray(params.years) ? params.years : [];
  const symbol = meta.symbol ?? params.tickers?.join(", ") ?? params.company?.ticker;
  // A native earnings page embeds date-observation charts frozen by the
  // event-dates calculator; identify that version by its own prefix.
  const dateVersion = typeof meta.calculation_version === "string"
    && meta.calculation_version.startsWith("event-dates-v");
  const versionNotice = kind === "earnings" && !dateVersion
    ? fiscalVersionNotice("native_earnings", meta.calculation_version)
    : dateVersion && kind === "earnings"
      ? fiscalVersionNotice("native_event_dates", meta.calculation_version)
      : kind === "event_dates"
        ? fiscalVersionNotice("event_import", meta.calculation_version)
        : null;
  const lines = [
    ...(versionNotice ? [versionNotice] : []),
    `证券 ${symbol || "待核对"} · ${meta.price_basis ?? "价格口径未知"} · 行情截至 ${meta.cutoff_date ?? "未知"}`,
    `研究窗口 ${window}${years.length ? ` · 历史年份 ${years.join("、")}` : params.historical_years ? ` · 历史目标 ${params.historical_years} 个完整年` : ""}`,
    ...notes,
  ];
  if (displayedSample) lines.push(`当前图仅展示：${displayedSample}；股票、基准及差值三条曲线不代表三个独立样本；缺失位置保留空白。`);
  const n = summary.n ?? data.effective_n;
  if (n != null) lines.push(`全研究完整历史有效 N=${n}${Number(n) === 1 ? "；仅单一样本，不能形成可靠规律或分布区间" : Number(n) === 0 ? "；没有有效历史样本" : ""}`);
  const current = summary.current;
  if (current) lines.push(`当前对照：${current.year ?? current.fiscal_year ?? "年份未知"} · ${current.status ?? "状态未知"}${current.actual_end ? ` · 实际截至 ${current.actual_end}` : ""}；不计入完整历史 N`);
  const path = Array.isArray(summary.path) ? summary.path : [];
  const counts = path.map((point: any) => Number(point.n)).filter(Number.isFinite);
  if (counts.length) lines.push(`全研究历史路径各位置有效 N=${Math.min(...counts)}—${Math.max(...counts)}；缺口位置可能不同${displayedSample ? "；不代表当前单一样本曲线的 N" : ""}`);
  const months = Array.isArray(summary.months) ? summary.months : [];
  if (months.length) {
    lines.push(`后端完整月有效 N（1—6月）：${months.slice(0, 6).map((m: any) => `${m.month}:${m.n ?? "未知"}`).join("  ")}`);
    lines.push(`后端完整月有效 N（7—12月）：${months.slice(6).map((m: any) => `${m.month}:${m.n ?? "未知"}`).join("  ")}`);
  }
  const distributions = Array.isArray(data.distributions) ? data.distributions : [];
  const distributionKind = data.kind ?? kind;
  const group = distributionKind === "monthly" ? String(params.month)
    : distributionKind === "interval" ? "interval"
    : params.date_category ?? `Q${params.quarter}`;
  const metric = distributionKind === "earnings" ? String(params.window ?? 5)
    : distributionKind === "event_dates" ? params.date_window ?? meta.date_window ?? "after5"
    : "endpoint";
  const selected = distributions.find((d: any) => d.metric === metric && String(d.group) === String(group));
  if (selected) lines.push(`所选组股票 N=${selected.stock?.n ?? "未知"} · 与基准同日配对 N=${selected.paired_stock?.n ?? "未知"}`);
  lines.push(...provenance.split("\n").filter(Boolean));
  lines.push("分位带仅描述冻结样本，非预测区间；默认仅拆股调整，不含股息再投资。");
  return lines;
}
