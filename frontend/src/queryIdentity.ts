/** Keys describe HTTP identity, not the consumer, tab or draft that reads it. */
export const researchKeys = {
  analysis: (id: string | null) => ["analysis", id ?? ""] as const,
  events: (id: string, result = "") => ["event-analysis", id, result] as const,
  sets: (kind: string) => ["event-sets", kind] as const,
  set: (id: string, version = "") => ["event-set", id, version] as const,
};

/** Commands and task progress of a research make its own reads stale. */
export function mutableResearchForBatch(key: readonly unknown[], analysisId?: string | null) {
  return !!analysisId && (key[0] === "analysis" || key[0] === "event-analysis") && key[1] === analysisId;
}
