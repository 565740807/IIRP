import { queryOptions, useQuery } from "@tanstack/react-query";
import { api, activeStatuses } from "./api";
import type { components } from "./generated/api";
import { researchKeys } from "./queryIdentity";

export type EventAnalysisOutput = components["schemas"]["EventAnalysisOutput"];
export type EventSetOutput = components["schemas"]["EventSetOutput"];

const interval = (status?: string) => status && activeStatuses.includes(status)
  ? (document.hidden ? 10000 : 2000) : false;
const read = <T>(path: string, signal: AbortSignal) => api<T>(path, "GET", undefined, signal);

export function eventResearchOptions(id: string) {
  return queryOptions({
    queryKey: researchKeys.events(id, ""),
    enabled: !!id,
    queryFn: ({ signal }) => read<EventAnalysisOutput>(`/events/analyses/${encodeURIComponent(id)}`, signal),
    staleTime: 5000,
    refetchInterval: q => interval(q.state.data?.status),
  });
}

export function eventSetsOptions(kind: string) {
  return queryOptions({
    queryKey: researchKeys.sets(kind),
    queryFn: ({ signal }) => read<components["schemas"]["EventSetsOutput"]>(`/events/sets?kind=${encodeURIComponent(kind)}`, signal),
    staleTime: 5000,
  });
}

export const useEventResearch = (id: string) => useQuery(eventResearchOptions(id));
export const useEventSets = (kind: string) => useQuery(eventSetsOptions(kind));
