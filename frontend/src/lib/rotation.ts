import { useQuery } from "@tanstack/react-query";
import i18n from "@/i18n";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import type { Level, RankBasis } from "./sectorState";

/**
 * Sector rotation: month-by-month (or one interval) ranks of the sectors or
 * the industry groups, computed by the backend from the long-term daily store.
 * Every number and rank comes from the server; this file only reads and labels.
 */
export type Rotation = Schemas["SectorRotation"];
export type RotationPeriod = Schemas["RotationPeriod"];
export type RotationDetail = Schemas["RotationDetail"];
export type RotationRow = Schemas["RotationRow"];
export type RotationCell = Schemas["RotationCell"];
export type RotationLeader = Schemas["RotationLeader"];

export type RotationChoice = {
  level: Level;
  rank: RankBasis;
  years: number;
  /** Monthly: the month shown in full (null for this month). */
  month?: number | null;
  /** Interval: start and end month-day. */
  interval?: { start: string; end: string } | null;
};

export function useRotation(choice: RotationChoice, enabled = true) {
  const query = {
    level: choice.level, rank_by: choice.rank, years: choice.years,
    ...(choice.interval ? { kind: "interval" as const, start: choice.interval.start, end: choice.interval.end }
      : { kind: "monthly" as const, ...(choice.month ? { month: choice.month } : {}) }),
  };
  return useQuery({
    queryKey: ["sector-rotation", query],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/sectors/rotation", { params: { query }, signal })),
    enabled,
    // Another month or basis keeps the table on screen until the new ranks arrive,
    // but never shows another level's rows.
    placeholderData: (previous) => previous?.level === choice.level && previous?.kind === query.kind ? previous : undefined,
    staleTime: 5 * 60 * 1000,
  });
}

/** The value a row is ranked by: its median change or its median excess over the S&P 500. */
export function rankedValue(stats: { median?: string | null; median_excess?: string | null } | null | undefined, rank: RankBasis) {
  return (rank === "absolute" ? stats?.median : stats?.median_excess) ?? null;
}

/** A year's value on the ranked basis. */
export function yearValue(entry: { change?: string | null; excess?: string | null }, rank: RankBasis) {
  return (rank === "absolute" ? entry.change : entry.excess) ?? null;
}

/** Display name of a sector or industry group. */
export function rotationName(level: Level, id: string, short = false) {
  return i18n.t(level === "group" ? `ui.sectors.group.${id}` : short ? `ui.sectors.short.${id}` : `ui.sectors.name.${id}`);
}
