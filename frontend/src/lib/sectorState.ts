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
/** The performance tables, the monthly rotation and the interval ranks. */
export const TABS = ["performance", "rotation", "interval"] as const;
export type Tab = (typeof TABS)[number];
/** Rotation ranks by the median change, or by the median excess over the S&P 500. */
export const RANK_BASES = ["absolute", "relative"] as const;
export type RankBasis = (typeof RANK_BASES)[number];
export const ROTATION_YEARS = [5, 8, 10] as const;
export type RotationYears = (typeof ROTATION_YEARS)[number];
/** "MM-DD" of a real calendar day (February 29 included). */
export function validMonthDay(value: string) {
  const match = /^(\d{2})-(\d{2})$/.exec(value);
  if (!match) return false;
  const [month, day] = [Number(match[1]), Number(match[2])];
  return month >= 1 && month <= 12 && day >= 1 && day <= new Date(Date.UTC(2000, month, 0)).getUTCDate();
}

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
  tab: Tab;
  rank: RankBasis;
  years: RotationYears;
  /** The month the rotation shows in full; null for this month. */
  month: number | null;
  /** Interval tab: start and end month-day ("MM-DD"); an end before the start crosses the year end. */
  start: string;
  end: string;
};

export const DEFAULT_STATE: SectorState = {
  level: "sector", sector: null, view: "table", period: "today", sort: "today", desc: true, selected: [], onlySelected: false,
  range: "3m", interval: "day", tab: "performance", rank: "absolute", years: 10, month: null, start: "09-20", end: "10-15",
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
    ...rotationState(params),
  };
}

function rotationState(params: URLSearchParams) {
  const years = Number(params.get("years"));
  const month = Number(params.get("month"));
  const start = params.get("start") ?? "";
  const end = params.get("end") ?? "";
  const valid = validMonthDay(start) && validMonthDay(end) && start !== end;
  return {
    tab: pick(params.get("tab"), TABS, DEFAULT_STATE.tab),
    rank: pick(params.get("rank"), RANK_BASES, DEFAULT_STATE.rank),
    years: (ROTATION_YEARS as readonly number[]).includes(years) ? years as RotationYears : DEFAULT_STATE.years,
    month: Number.isInteger(month) && month >= 1 && month <= 12 ? month : null,
    start: valid ? start : DEFAULT_STATE.start,
    end: valid ? end : DEFAULT_STATE.end,
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
  if (state.tab !== DEFAULT_STATE.tab) params.set("tab", state.tab);
  if (state.rank !== DEFAULT_STATE.rank) params.set("rank", state.rank);
  if (state.years !== DEFAULT_STATE.years) params.set("years", String(state.years));
  if (state.month != null) params.set("month", String(state.month));
  if (state.start !== DEFAULT_STATE.start || state.end !== DEFAULT_STATE.end) {
    params.set("start", state.start);
    params.set("end", state.end);
  }
  return params.toString();
}

/** Links that open the monthly or interval analysis for these ETFs. */
export function analysisLinks(etfs: readonly string[]) {
  const tickers = encodeURIComponent(etfs.join(","));
  return { monthly: `/analysis/monthly?tickers=${tickers}`, interval: `/analysis/interval?tickers=${tickers}` };
}

/** The monthly (or interval) analysis of one ETF over the rotation's years, opened on its month. */
export function rotationAnalysisLink(etf: string, years: number, choice: { month?: number | null; start?: string; end?: string } = {}) {
  const params = new URLSearchParams({ tickers: etf, years: String(years) });
  if (choice.start && choice.end) {
    params.set("start", choice.start);
    params.set("end", choice.end);
    return `/analysis/interval?${params}`;
  }
  if (choice.month) params.set("m", String(choice.month));
  return `/analysis/monthly?${params}`;
}

