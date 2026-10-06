/** Keys describe HTTP identity, not the consumer, tab or draft that reads it. */
export function canonicalResults(ids: string) {
  // Keep malformed IDs malformed: never turn an invalid fixed URL into latest.
  if (!ids || ids.split(",").some(id => !id)) return ids;
  return [...new Set(ids.split(","))].sort().join(",");
}

export const researchKeys = {
  native: (id: string | null, results = "") =>
    ["analysis", id ?? "", canonicalResults(results)] as const,
  events: (id: string, result = "") => ["event-analysis", id, result] as const,
  sets: (kind: string) => ["event-sets", kind] as const,
  set: (id: string, version = "") => ["event-set", id, version] as const,
  recent: (kind: string, ticker: string) => ["recent-analyses", kind, ticker] as const,
  batch: (id: string) => ["batch", id] as const,
};

export function isFrozenResearchQuery(key: readonly unknown[]) {
  return (key[0] === "analysis" || key[0] === "event-analysis") && !!key[2];
}

/** Commands and task progress cannot make an immutable result stale. */
export function mutableResearchForBatch(key: readonly unknown[], analysisId?: string | null) {
  return !!analysisId && (key[0] === "analysis" || key[0] === "event-analysis") &&
    key[1] === analysisId && !isFrozenResearchQuery(key);
}
