import { queryOptions, useQuery } from "@tanstack/react-query";
import { api, activeStatuses, type AnalysisOutput, type CollectionOutput } from "./api";
import type { components } from "./generated/api";
import { mergeAnalysisResults } from "./analysisProgress";
import { canonicalResults, researchKeys } from "./queryIdentity";

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

export function nativeResearchOptions(id: string | null, results = "") {
  const frozen = canonicalResults(results);
  return queryOptions({
    queryKey: researchKeys.native(id, frozen),
    enabled: !!id,
    queryFn: ({ signal }) => read<AnalysisOutput>(
      `/analyses/${encodeURIComponent(id!)}?${new URLSearchParams({ result_ids: frozen })}`, signal),
    staleTime: frozen ? "static" : 5000,
    structuralSharing: (old, next) => mergeAnalysisResults(old as AnalysisOutput | undefined, next as AnalysisOutput, frozen),
    refetchInterval: q => frozen ? false : interval(q.state.data?.status),
  });
}

export function batchOptions(id: string) {
  return queryOptions({
    queryKey: researchKeys.batch(id),
    enabled: !!id,
    queryFn: ({ signal }) => read<CollectionOutput>(`/batches/${encodeURIComponent(id)}`, signal),
    staleTime: 5000,
    refetchInterval: q => interval(q.state.data?.batch.status),
  });
}

export function eventSetOptions(id: string) {
  return queryOptions({
    queryKey: researchKeys.set(id, ""),
    enabled: !!id,
    queryFn: ({ signal }) => read<EventSetOutput>(`/events/sets/${encodeURIComponent(id)}`, signal),
    staleTime: 5000,
  });
}

export function eventSetsOptions(kind: string) {
  return queryOptions({
    queryKey: researchKeys.sets(kind),
    queryFn: ({ signal }) => read<components["schemas"]["EventSetsOutput"]>(`/events/sets?kind=${encodeURIComponent(kind)}`, signal),
    staleTime: 5000,
  });
}

export function recentResearchOptions(kind: string, ticker: string) {
  return queryOptions({
    queryKey: researchKeys.recent(kind, ticker),
    queryFn: ({ signal }) => read<components["schemas"]["RecentAnalysesOutput"]>(
      `/analyses?${new URLSearchParams({ kind, ticker })}`, signal),
    staleTime: 5000,
  });
}

export const useEventResearch = (id: string) => useQuery(eventResearchOptions(id));
export const useNativeResearch = (id: string | null, results = "") => useQuery(nativeResearchOptions(id, results));
export const useResearchBatch = (id: string) => useQuery(batchOptions(id));
export const useEventSet = (id: string) => useQuery(eventSetOptions(id));
export const useEventSets = (kind: string) => useQuery(eventSetsOptions(kind));
export const useRecentResearch = (kind: string, ticker: string) => useQuery(recentResearchOptions(kind, ticker));
