import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { client, unwrap, type Schemas } from "@/lib/api-client";
import { usePageVisible, useRefreshIntervals } from "@/lib/refresh";

export type InsiderActivity = Schemas["InsiderActivity"];

/**
 * Open-market Insider activity of one batch of codes (lib/tickerMarks), read
 * once for the whole batch and shared by every mark of it; local reads only,
 * re-read at the overview's pace while the page is visible.
 */
export function useInsiderActivity(batch: readonly string[], days = 30) {
  const visible = usePageVisible();
  const intervals = useRefreshIntervals();
  const tickers = batch.join(",");
  return useQuery({
    queryKey: ["insider-activity", days, tickers],
    queryFn: ({ signal }) => unwrap(client.GET("/api/v1/insider/activity", { params: { query: { tickers, days } }, signal })),
    enabled: !!tickers,
    staleTime: 30_000,
    refetchInterval: visible ? intervals.overview_poll_seconds * 1000 : false,
    placeholderData: keepPreviousData,
  });
}
