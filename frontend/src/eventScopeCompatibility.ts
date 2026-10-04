/** Old expanded years do not record whether the user explicitly chose them.
 * Detect incompatibility, never silently intersect or infer default intent. */
type Values = Record<string, any>;
function importedRange(scope: Values) {
  const start = scope.fiscal_year_start ?? scope.year_start;
  const end = scope.fiscal_year_end ?? scope.year_end;
  return Number.isInteger(start) && Number.isInteger(end) && start >= 1 && end >= start && end <= 9998
    ? { start: Number(start), end: Number(end) }
    : null;
}
export function eventScopeConflict(params: Values, scope: Values): { start: number; end: number; outsideYears: number[] } | null {
  const range = importedRange(scope);
  if (!range || !Array.isArray(params.years) || !params.years.every(Number.isInteger)) return null;
  const outsideYears = [...new Set<number>(params.years.filter((year: number) => year < range.start || year > range.end))];
  return outsideYears.length ? { ...range, outsideYears } : null;
}

/** Call only after the user chooses a NEW study within the imported scope.
 * Omit years/cutoff so the backend derives the scope and latest completed day;
 * retain the immutable event version, exclusions and all other study choices. */
export function eventAnalysisForImportedScope(params: Values, scope: Values): Values {
  const range = importedRange(scope);
  if (!range) throw new Error("事件资料范围未知，无法按导入范围新建研究");
  return {
    ...Object.fromEntries([
      "current_fiscal_year", "include_unverified", "common_years", "benchmark",
      "excluded_years", "date_window", "date_category",
    ].filter((key) => params[key] !== undefined).map((key) => [key, params[key]])),
    version: params.event_version,
    historical_years: range.end - range.start + 1,
  };
}
