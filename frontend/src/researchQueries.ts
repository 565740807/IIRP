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

export function eventResearchOptions(id: string, result = "") {
  return queryOptions({
    queryKey: researchKeys.events(id, result),
    enabled: !!id,
    queryFn: ({ signal }) => read<EventAnalysisOutput>(
      `/events/analyses/${encodeURIComponent(id)}${result ? `?result_id=${encodeURIComponent(result)}` : ""}`, signal),
    staleTime: result ? "static" : 5000,
    refetchInterval: q => result ? false : interval(q.state.data?.status),
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

export function eventSetOptions(id: string, version = "") {
  return queryOptions({
    queryKey: researchKeys.set(id, version),
    enabled: !!id,
    queryFn: ({ signal }) => read<EventSetOutput>(
      `/events/sets/${encodeURIComponent(id)}${version ? `?version=${encodeURIComponent(version)}` : ""}`, signal),
    // The facts are versioned, but the envelope also contains a live research list.
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

export function overlapOptions(analysis: string, result: string, event: string, cursor: string | null, limit = 50) {
  return queryOptions({
    queryKey: researchKeys.overlaps(analysis, result, event, cursor, limit),
    enabled: !!analysis && !!result,
    staleTime: "static",
    retry: false,
    queryFn: ({ signal }) => read<components["schemas"]["EventOverlapPage"]>(
      `/analyses/${encodeURIComponent(analysis)}/results/${encodeURIComponent(result)}/event-overlaps?${new URLSearchParams({ event_key: event, limit: String(limit), ...(cursor ? { cursor } : {}) })}`, signal),
  });
}

export const useEventResearch = (id: string, result = "") => useQuery(eventResearchOptions(id, result));
export const useNativeResearch = (id: string | null, results = "") => useQuery(nativeResearchOptions(id, results));
export const useResearchBatch = (id: string) => useQuery(batchOptions(id));
export const useEventSet = (id: string, version = "") => useQuery(eventSetOptions(id, version));
export const useEventSets = (kind: string) => useQuery(eventSetsOptions(kind));
export const useRecentResearch = (kind: string, ticker: string) => useQuery(recentResearchOptions(kind, ticker));
