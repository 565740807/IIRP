/**
 * Sector page choices and their URL form; the level, the sector drilled into,
 * every view, period, sort and selection live in the link.
 */

export const PERIODS = ["today", "1w", "1m", "3m", "ytd"] as const;
export type Period = (typeof PERIODS)[number];
export const VIEWS = ["table", "heatmap", "kline"] as const;
export type View = (typeof VIEWS)[number];
export const RANGES = ["3m", "6m", "1y", "ytd"] as const;
export type ChartRange = (typeof RANGES)[number];
export const INTERVALS = ["day", "week", "month"] as const;
export type Interval = (typeof INTERVALS)[number];
export const SORT_COLUMNS = ["name", "etf", ...PERIODS] as const;
export type SortColumn = (typeof SORT_COLUMNS)[number];
export const LEVELS = ["sector", "group"] as const;
export type Level = (typeof LEVELS)[number];
/** One analysis takes at most this many ETFs. */
export const MAX_ANALYSIS_ETFS = 20;

export type SectorState = {
  level: Level;
  /** The sector whose industry groups are shown; null for all of them. */
  sector: string | null;
  view: View;
  period: Period;
  sort: SortColumn;
  desc: boolean;
  selected: string[];
  onlySelected: boolean;
  range: ChartRange;
  interval: Interval;
};

export const DEFAULT_STATE: SectorState = {
  level: "sector", sector: null, view: "table", period: "today", sort: "today", desc: true, selected: [], onlySelected: false,
  range: "3m", interval: "day",
};

function pick<T extends string>(value: string | null, options: readonly T[], fallback: T): T {
  return options.includes(value as T) ? (value as T) : fallback;
}

/**
 * Page state from the URL; unknown values fall back to the defaults. ``etfs``
 * are the ETFs of both levels, so a selection made on one level survives the
 * other; ``sectors`` are the sector ids a drill-down may name.
 */
export function parseSectorState(params: URLSearchParams, etfs: readonly string[] = [], sectors: readonly string[] = []): SectorState {
  const [column, direction] = (params.get("sort") ?? "").split(".");
  const valid = SORT_COLUMNS.includes(column as SortColumn);
  const known = new Set(etfs);
  const selected = (params.get("selected") ?? "").split(",").map((value) => value.trim().toUpperCase())
    .filter((value) => value && (!known.size || known.has(value)));
  const level = pick(params.get("level"), LEVELS, DEFAULT_STATE.level);
  const sector = params.get("sector");
  return {
    level,
    sector: level === "group" && sector && (!sectors.length || sectors.includes(sector)) ? sector : null,
    view: pick(params.get("view"), VIEWS, DEFAULT_STATE.view),
    period: pick(params.get("period"), PERIODS, DEFAULT_STATE.period),
    sort: valid ? (column as SortColumn) : DEFAULT_STATE.sort,
    desc: valid ? direction !== "asc" : DEFAULT_STATE.desc,
    selected: [...new Set(selected)],
    onlySelected: params.get("only") === "1" && selected.length > 0,
    range: pick(params.get("range"), RANGES, DEFAULT_STATE.range),
    interval: pick(params.get("interval"), INTERVALS, DEFAULT_STATE.interval),
  };
}

/** URL search for a state; defaults are left out so shared links stay short. */
export function sectorSearch(state: SectorState): string {
  const params = new URLSearchParams();
  if (state.level !== DEFAULT_STATE.level) params.set("level", state.level);
  if (state.level === "group" && state.sector) params.set("sector", state.sector);
  if (state.view !== DEFAULT_STATE.view) params.set("view", state.view);
  if (state.period !== DEFAULT_STATE.period) params.set("period", state.period);
  if (state.sort !== DEFAULT_STATE.sort || state.desc !== DEFAULT_STATE.desc) {
    params.set("sort", `${state.sort}.${state.desc ? "desc" : "asc"}`);
  }
  if (state.selected.length) params.set("selected", state.selected.join(","));
  if (state.onlySelected && state.selected.length) params.set("only", "1");
  if (state.range !== DEFAULT_STATE.range) params.set("range", state.range);
  if (state.interval !== DEFAULT_STATE.interval) params.set("interval", state.interval);
  return params.toString();
}

/** Links that open the monthly or interval analysis for these ETFs. */
export function analysisLinks(etfs: readonly string[]) {
  const tickers = encodeURIComponent(etfs.join(","));
  return { monthly: `/analysis/monthly?tickers=${tickers}`, interval: `/analysis/interval?tickers=${tickers}` };
}

