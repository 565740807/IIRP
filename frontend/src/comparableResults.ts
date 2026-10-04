import type { AnalysisItem } from "./api";

/** A shared chart only contains current results with the same frozen contract. */
export function comparableResults(
  results: AnalysisItem[],
  selected: AnalysisItem | undefined,
  tickers: string[],
): AnalysisItem[] {
  if (!selected?.is_current) return [];
  const base = selected.data.metadata;
  if (!base?.calculation_version || !base?.cutoff_date || !base?.price_basis) return [];
  return results.filter((row) => {
    const meta = row.data.metadata;
    return tickers.includes(row.symbol) && row.is_current &&
      meta?.calculation_version === base.calculation_version &&
      meta?.cutoff_date === base.cutoff_date &&
      meta?.price_basis === base.price_basis &&
      meta?.comparison === base.comparison &&
      meta?.calendar_version === base.calendar_version;
  }).slice(0, 5);
}
