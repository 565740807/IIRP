import type { Fact } from "./api";

// Only the backend decides event eligibility and computes returns. This view
// keeps date-only observations out of the precise reaction presentation.
export function preciseEarningsView(data: Fact): Fact {
  const rows = Array.isArray(data.rows) ? data.rows : [];
  const preciseRows = rows.filter((row: Fact) => row.precise === true);
  const preciseIds = new Set(preciseRows.map((row: Fact) => String(row.event_id)));
  const summary = data.summary ?? {};
  return {
    ...data,
    rows: preciseRows,
    cells: (Array.isArray(data.cells) ? data.cells : []).filter(
      (cell: Fact) => cell.precise === true,
    ),
    series: (Array.isArray(data.series) ? data.series : []).filter(
      (series: Fact) => preciseIds.has(String(series.key)),
    ),
    distributions: (Array.isArray(data.distributions) ? data.distributions : []).map(
      (entry: Fact) => ({
        ...entry,
        samples: (Array.isArray(entry.samples) ? entry.samples : []).filter(
          (sample: Fact) => preciseIds.has(String(sample.key)),
        ),
      }),
    ),
    summary: {
      ...summary,
      current: summary.current?.precise === true ? summary.current : null,
    },
  };
}

export function selectedDateObservationN(
  data: Fact,
  quarter: number,
  dateWindow: string,
  category?: string | null,
): number | null {
  const observations = data.date_observation;
  if (!observations || !Array.isArray(observations.distributions)) return null;
  const group = category === "unassigned_earnings" ? category : `Q${quarter}`;
  const selected = observations.distributions.find(
    (item: Fact) => item.group === group && item.metric === dateWindow,
  );
  const n = selected?.stock?.n;
  return typeof n === "number" && Number.isFinite(n) ? n : null;
}
