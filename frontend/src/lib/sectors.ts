import type { Schemas } from "@/lib/api-client";
import type { Period } from "./sectorState";

export * from "./sectorState";

export type SectorPerformance = Schemas["SectorPerformance"];
export type SectorItem = Schemas["SectorItem"];
export type SectorChange = Schemas["SectorChange"];
export type SectorCandles = Schemas["SectorCandlesOutput"];
export type SectorGroups = Schemas["SectorGroups"];
export type SectorGroupItem = Schemas["SectorGroupItem"];
export type EtfPerformance = Schemas["EtfPerformance"];
export type PriceFetch = Schemas["PriceFetch"];
/** A row that has an ETF and its periods: a sector, a ranked group or a reference row. */
export type PricedRow = { id: string; etf: string; periods: SectorItem["periods"]; weights?: SectorItem["weights"] | null; first_date?: string | null; full_years: number };

/** Response field of each period code. */
export const PERIOD_FIELD = { today: "today", "1w": "week", "1m": "month", "3m": "quarter", ytd: "ytd" } as const;

export function periodChange(item: Pick<PricedRow, "periods">, period: Period): SectorChange {
  return item.periods[PERIOD_FIELD[period]];
}

export function periodWeight(item: PricedRow, period: Period) {
  return item.weights?.[PERIOD_FIELD[period]] ?? null;
}

/** The backend's rank of a group for a period (1 = largest change); null when not ranked. */
export function periodRank(item: SectorGroupItem, period: Period) {
  return item.ranks?.[PERIOD_FIELD[period]] ?? null;
}

/** Groups with a primary ETF, as rows the heatmap and the charts take. */
export function rankedGroups(items: readonly SectorGroupItem[]): (SectorGroupItem & PricedRow)[] {
  return items.filter((item): item is SectorGroupItem & PricedRow => item.etf != null && item.periods != null);
}

export { useSectors, useSectorGroups, useSectorCandles } from "./sectorQueries";
