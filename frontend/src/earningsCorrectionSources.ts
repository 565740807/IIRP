export type CorrectionSources = {
  source_url: string;
  period_kind: "regular" | "transition" | "unknown";
  period_kind_source_url: string | null;
  period_kind_evidence: string | null;
  time_evidence: string | null;
};

type Evidence = Record<string, unknown>;

function records(value: unknown): Evidence[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is Evidence => entry !== null && typeof entry === "object" && !Array.isArray(entry))
    : [];
}

function string(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

/** Keep separate date and fiscal-kind attestations on an existing native fact. */
export function correctionSources(event: Evidence): CorrectionSources {
  const evidence = records(event.evidence);
  const samePeriod = (item: Evidence) =>
    (item.fiscal_year === undefined || item.fiscal_year === event.fiscal_year) &&
    (item.fiscal_quarter === undefined || item.fiscal_quarter === event.fiscal_quarter);
  const dateReview = [...evidence].reverse().find((item) =>
    item.provider === "manual_review" &&
    item.period_kind !== "regular" && item.period_kind !== "transition" && samePeriod(item) &&
    item.announced_date === event.announced_date && string(item.source_url));
  const dateSource = dateReview ?? [...evidence].reverse().find((item) =>
    item.period_kind !== "regular" && item.period_kind !== "transition" &&
    samePeriod(item) && item.announced_date === event.announced_date && string(item.source_url));
  const source_url = string(dateSource?.source_url) ?? string(event.source_url) ?? "";

  const manualKinds = evidence.filter((item) => item.provider === "manual_period_review");
  const candidates = manualKinds.length ? [manualKinds.at(-1)!] : evidence;
  const supported = candidates.filter((item) =>
    event.fiscal_year !== undefined && event.fiscal_year !== null &&
    event.fiscal_quarter !== undefined && event.fiscal_quarter !== null &&
    item.fiscal_year === event.fiscal_year &&
    item.fiscal_quarter === event.fiscal_quarter &&
    item.provider !== "manual_review" &&
    (item.period_kind === "regular" || item.period_kind === "transition") &&
    string(item.source_url) && string(item.period_kind_evidence));
  const kinds = new Set(supported.map((item) => item.period_kind));
  const selected = kinds.size === 1 ? supported.at(-1) : undefined;
  return {
    source_url,
    period_kind: selected?.period_kind as CorrectionSources["period_kind"] ?? "unknown",
    period_kind_source_url: string(selected?.source_url),
    period_kind_evidence: string(selected?.period_kind_evidence),
    time_evidence: string(dateReview?.time_evidence),
  };
}
